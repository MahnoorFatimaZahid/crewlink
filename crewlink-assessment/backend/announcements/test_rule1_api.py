"""
Rule 1 through the real API, the way a reviewer (or an attacker) would try it.
Each test builds its own small world, so it doesn't depend on the seed data.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from announcements.models import Announcement, AnnouncementRecipient
from announcements.serializers import AnnouncementSerializer
from core.models import Account, Local, Member
from core.scoping import LocalBoundaryViolation

User = get_user_model()
LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def make_member(local, name, classification="Journeyman Wireman", status="active"):
    return Member.objects.create(local=local, full_name=name, email=f"{name}@example.com",
                                 classification=classification, status=status)


def make_account(username, local, role, member=None):
    user = User.objects.create_user(username=username, password="pass12345")
    account = Account.objects.create(user=user, local=local, role=role, member=member)
    return account, Token.objects.create(user=user).key


@override_settings(CACHES=LOCMEM)  # keep the login throttle out of the dev Redis
class Rule1Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.l27 = Local.objects.create(name="Local 27")
        cls.l12 = Local.objects.create(name="Local 12")

        cls.m27 = make_member(cls.l27, "ana")
        cls.m27_other = make_member(cls.l27, "ben")
        cls.m12 = make_member(cls.l12, "cy")

        cls.leader27, cls.t_leader27 = make_account("leader27", cls.l27, "leader")
        cls.leader12, cls.t_leader12 = make_account("leader12", cls.l12, "leader")
        cls.member27, cls.t_member27 = make_account("member27", cls.l27, "member", cls.m27)
        cls.member27b, cls.t_member27b = make_account("member27b", cls.l27, "member", cls.m27_other)
        cls.member12, cls.t_member12 = make_account("member12", cls.l12, "member", cls.m12)

        cls.ann27 = Announcement.objects.create(
            local=cls.l27, title="Emergency meeting", body="Thursday 6pm at the hall",
            push_preview="Emergency meeting Thu 6pm", needs_ack=True, created_by=cls.leader27)
        cls.ann12 = Announcement.objects.create(
            local=cls.l12, title="Local 12 news", body="...", push_preview="Local 12 news",
            created_by=cls.leader12)

    def client_for(self, token=None):
        c = APIClient()
        if token:
            c.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        return c

    def new_draft(self, client, **extra):
        payload = {"title": "T", "body": "B", "push_preview": "P", **extra}
        return client.post("/api/announcements", payload, format="json")


class LoginTests(Rule1Base):
    def setUp(self):
        # The throttle counts logins in the cache. Without this, the throttle test's
        # 11 attempts would leak into the other tests and make them fail with 429.
        # Safe to clear: Rule1Base points the cache at local memory, not your Redis.
        cache.clear()

    def test_login_returns_token_role_and_local(self):
        r = self.client_for().post("/api/auth/login",
                                   {"username": "leader27", "password": "pass12345"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["role"], "leader")
        self.assertEqual(r.data["local_id"], str(self.l27.id))

    def test_wrong_password_and_unknown_user_look_identical(self):
        c = self.client_for()
        wrong = c.post("/api/auth/login", {"username": "leader27", "password": "nope"}, format="json")
        unknown = c.post("/api/auth/login", {"username": "ghost", "password": "nope"}, format="json")
        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(wrong.data, unknown.data)  # doesn't reveal which usernames exist

    def test_user_without_account_cannot_log_in(self):
        User.objects.create_user(username="orphan", password="pass12345")
        r = self.client_for().post("/api/auth/login",
                                   {"username": "orphan", "password": "pass12345"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_login_is_throttled(self):
        c = self.client_for()
        codes = [c.post("/api/auth/login", {"username": "x", "password": "y"}, format="json").status_code
                 for _ in range(11)]
        self.assertEqual(codes[-1], 429)


class NoTokenTests(Rule1Base):
    def test_every_protected_endpoint_needs_a_token(self):
        c = self.client_for()
        for method, url in [
            ("get", "/api/me"),
            ("get", "/api/announcements"),
            ("post", "/api/announcements"),
            ("get", f"/api/announcements/{self.ann27.id}"),
            ("post", f"/api/announcements/{self.ann27.id}/approve"),
            ("get", "/api/me/announcements"),
            ("post", f"/api/me/announcements/{self.ann27.id}/ack"),
        ]:
            with self.subTest(method=method, url=url):
                self.assertEqual(getattr(c, method)(url).status_code, 401)

    def test_bad_token_is_rejected(self):
        self.assertEqual(self.client_for("not-a-real-token").get("/api/me").status_code, 401)


class BetweenLocalsTests(Rule1Base):
    """The floor boundary: another local's data is never found (404), never listed."""

    def test_leader_list_shows_only_own_local(self):
        r = self.client_for(self.t_leader27).get("/api/announcements")
        ids = {a["id"] for a in r.data}
        self.assertIn(str(self.ann27.id), ids)
        self.assertNotIn(str(self.ann12.id), ids)

    def test_leader_cannot_read_edit_or_approve_another_locals_announcement(self):
        c = self.client_for(self.t_leader27)
        url = f"/api/announcements/{self.ann12.id}"
        self.assertEqual(c.get(url).status_code, 404)
        self.assertEqual(c.patch(url, {"title": "hacked"}, format="json").status_code, 404)
        self.assertEqual(c.post(url + "/approve", {"version": 1}, format="json").status_code, 404)
        self.ann12.refresh_from_db()
        self.assertEqual(self.ann12.title, "Local 12 news")
        self.assertEqual(self.ann12.status, "draft")

    def test_404_for_other_local_matches_404_for_nonexistent(self):
        c = self.client_for(self.t_leader27)
        other = c.get(f"/api/announcements/{self.ann12.id}")
        missing = c.get("/api/announcements/00000000-0000-0000-0000-000000000000")
        self.assertEqual((other.status_code, other.data), (missing.status_code, missing.data))

    def test_local_in_request_body_is_ignored(self):
        r = self.new_draft(self.client_for(self.t_leader27), local=str(self.l12.id))
        self.assertEqual(r.status_code, 201)
        created = Announcement.objects.get(pk=r.data["id"])
        self.assertEqual(created.local_id, self.l27.id)
        self.assertEqual(created.created_by_id, self.leader27.id)

    def test_cannot_supersede_another_locals_announcement(self):
        r = self.new_draft(self.client_for(self.t_leader27), supersedes=str(self.ann12.id))
        self.assertEqual(r.status_code, 400)

    def test_member_of_other_local_gets_404_on_inbox_item(self):
        AnnouncementRecipient.objects.create(announcement=self.ann27, member=self.m27, local=self.l27)
        c = self.client_for(self.t_member12)
        self.assertEqual(c.get(f"/api/me/announcements/{self.ann27.id}").status_code, 404)
        self.assertEqual(c.post(f"/api/me/announcements/{self.ann27.id}/ack").status_code, 404)


class MemberVsLeaderTests(Rule1Base):
    """The room boundary: a member never gets a leader's powers, and vice versa."""

    def test_member_gets_403_on_every_leader_endpoint(self):
        c = self.client_for(self.t_member27)
        self.assertEqual(c.get("/api/announcements").status_code, 403)
        self.assertEqual(self.new_draft(c).status_code, 403)
        self.assertEqual(c.get(f"/api/announcements/{self.ann27.id}").status_code, 403)
        self.assertEqual(c.post(f"/api/announcements/{self.ann27.id}/approve",
                                {"version": 1}, format="json").status_code, 403)
        self.ann27.refresh_from_db()
        self.assertEqual(self.ann27.status, "draft")

    def test_leader_has_no_inbox(self):
        self.assertEqual(self.client_for(self.t_leader27).get("/api/me/announcements").status_code, 403)


class MemberInboxTests(Rule1Base):
    def setUp(self):
        self.row = AnnouncementRecipient.objects.create(
            announcement=self.ann27, member=self.m27, local=self.l27)

    def test_member_only_sees_own_rows(self):
        AnnouncementRecipient.objects.create(announcement=self.ann12, member=self.m12, local=self.l12)
        r = self.client_for(self.t_member27).get("/api/me/announcements")
        self.assertEqual([i["announcement_id"] for i in r.data], [str(self.ann27.id)])

    def test_colleague_in_same_local_who_wasnt_a_recipient_gets_404(self):
        # ben is in Local 27 too, but this callout wasn't sent to him.
        c = self.client_for(self.t_member27b)
        self.assertEqual(c.get(f"/api/me/announcements/{self.ann27.id}").status_code, 404)

    def test_inbox_never_exposes_contact_details(self):
        r = self.client_for(self.t_member27).get("/api/me/announcements")
        text = str(r.content)
        self.assertNotIn("@example.com", text)
        self.assertNotIn("email", text)
        self.assertNotIn("member", r.data[0])

    def test_syncing_inbox_sets_received_at(self):
        self.client_for(self.t_member27).get("/api/me/announcements")
        self.row.refresh_from_db()
        self.assertIsNotNone(self.row.received_at)
        self.assertIsNone(self.row.read_at)

    def test_ack_also_marks_read_and_repeat_keeps_first_time(self):
        c = self.client_for(self.t_member27)
        first = c.post(f"/api/me/announcements/{self.ann27.id}/ack")
        self.assertEqual(first.status_code, 200)
        self.row.refresh_from_db()
        first_ack, first_read = self.row.acked_at, self.row.read_at
        self.assertIsNotNone(first_read)
        c.post(f"/api/me/announcements/{self.ann27.id}/ack")
        self.row.refresh_from_db()
        self.assertEqual((self.row.acked_at, self.row.read_at), (first_ack, first_read))

    def test_ack_refused_when_callout_does_not_ask_for_it(self):
        self.ann27.needs_ack = False
        self.ann27.save()
        r = self.client_for(self.t_member27).post(f"/api/me/announcements/{self.ann27.id}/ack")
        self.assertEqual(r.status_code, 400)

    def test_malformed_id_is_404_not_500(self):
        r = self.client_for(self.t_member27).get("/api/me/announcements/not-a-uuid")
        self.assertEqual(r.status_code, 404)


class ApprovalGateTests(Rule1Base):
    def approve(self, client, ann_id, version):
        return client.post(f"/api/announcements/{ann_id}/approve", {"version": version}, format="json")

    def test_editing_clears_approval_and_bumps_version(self):
        c = self.client_for(self.t_leader27)
        self.assertEqual(self.approve(c, self.ann27.id, 1).status_code, 200)
        r = c.patch(f"/api/announcements/{self.ann27.id}", {"body": "Now 7pm"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.data["status"], r.data["version"], r.data["approved_by"]), ("draft", 2, None))

    def test_approving_a_version_you_did_not_see_is_refused(self):
        c = self.client_for(self.t_leader27)
        c.patch(f"/api/announcements/{self.ann27.id}", {"body": "changed by someone else"}, format="json")
        r = self.approve(c, self.ann27.id, 1)  # still looking at version 1
        self.assertEqual(r.status_code, 409)
        self.ann27.refresh_from_db()
        self.assertEqual(self.ann27.status, "draft")

    def test_repeat_approve_of_same_version_is_harmless(self):
        c = self.client_for(self.t_leader27)
        self.assertEqual(self.approve(c, self.ann27.id, 1).status_code, 200)
        self.assertEqual(self.approve(c, self.ann27.id, 1).status_code, 200)

    def test_approve_without_version_is_400(self):
        r = self.client_for(self.t_leader27).post(f"/api/announcements/{self.ann27.id}/approve")
        self.assertEqual(r.status_code, 400)

    def test_cannot_edit_once_queued(self):
        Announcement.objects.filter(pk=self.ann27.pk).update(
            status="queued", approved_by=self.leader27, approved_at=timezone.now())
        r = self.client_for(self.t_leader27).patch(
            f"/api/announcements/{self.ann27.id}", {"title": "too late"}, format="json")
        self.assertEqual(r.status_code, 409)

    def test_malformed_id_on_edit_and_approve_is_404_not_500(self):
        c = self.client_for(self.t_leader27)
        self.assertEqual(c.patch("/api/announcements/not-a-uuid", {"title": "x"},
                                 format="json").status_code, 404)
        self.assertEqual(self.approve(c, "not-a-uuid", 1).status_code, 404)

    def test_client_cannot_set_status_or_approver_directly(self):
        c = self.client_for(self.t_leader27)
        r = c.patch(f"/api/announcements/{self.ann27.id}",
                    {"status": "approved", "approved_by": self.leader27.id, "version": 99},
                    format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.data["status"], r.data["approved_by"]), ("draft", None))
        self.assertEqual(r.data["version"], 2)


class ValidationTests(Rule1Base):
    def test_audience_matching_nobody_is_refused(self):
        r = self.new_draft(self.client_for(self.t_leader27), audience_classification="Apprentce (typo)")
        self.assertEqual(r.status_code, 400)

    def test_retired_members_do_not_count_as_an_audience(self):
        make_member(self.l27, "old", classification="Retiree Group", status="retired")
        r = self.new_draft(self.client_for(self.t_leader27), audience_classification="Retiree Group")
        self.assertEqual(r.status_code, 400)

    def test_meeting_in_the_past_is_refused(self):
        r = self.new_draft(self.client_for(self.t_leader27), kind="meeting",
                           event_at=(timezone.now() - timedelta(hours=1)).isoformat())
        self.assertEqual(r.status_code, 400)

    def test_push_preview_over_120_chars_is_refused_by_api(self):
        r = self.new_draft(self.client_for(self.t_leader27), push_preview="x" * 121)
        self.assertEqual(r.status_code, 400)


class TripwireTests(Rule1Base):
    def test_tripwire_blocks_an_object_from_another_local(self):
        # Simulate a bug that slipped past the scoping: serialize Local 12's
        # announcement for a Local 27 leader.
        request = type("Req", (), {"user": self.leader27.user, "path": "/test"})()
        with self.assertLogs("crewlink.security", level="CRITICAL"):
            with self.assertRaises(LocalBoundaryViolation):
                AnnouncementSerializer(self.ann12, context={"request": request}).data

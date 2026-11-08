"""
The AI helper. What's scored is the integration: slow or failing providers, and
whether generated text can reach members without a human approving it.
No test calls the real Gemini API.
"""
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from announcements import ai
from announcements.models import Announcement
from announcements.views import AIDraftThrottle
from core.models import Account, Local, Member

User = get_user_model()
LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
NOTE = ("emergency mtg thurs 6pm hall re: contractor pulling crews off the westside job, "
        "EVERYONE needs to be there this is the third time")


class FakeGeminiClient:
    """Stands in for google.genai.Client: records the call, returns or raises."""

    def __init__(self, text=None, exc=None):
        self.calls = []
        self.models = SimpleNamespace(generate_content=self._generate)
        self.text, self.exc = text, exc

    def _generate(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return SimpleNamespace(text=self.text)


def gemini(text=None, exc=None):
    client = FakeGeminiClient(text=text, exc=exc)
    return ai.GeminiDrafter(api_key="unused", model="gemini-test", timeout_s=10, client=client), client


GOOD = json.dumps({"title": "Emergency meeting Thursday", "body": "Thursday 6pm at the hall.",
                   "push_preview": "Emergency meeting Thursday 6pm at the hall"})


# ---------------------------------------------------------- provider unit tests

class GeminiDrafterTests(TestCase):
    def test_only_the_note_is_sent_to_the_provider(self):
        drafter, client = gemini(text=GOOD)
        drafter.draft(NOTE)
        call = client.calls[0]
        self.assertEqual(call["contents"], NOTE)
        self.assertEqual(call["model"], "gemini-test")
        self.assertEqual(call["config"].response_mime_type, "application/json")

    def test_timeout_becomes_ai_unavailable_and_the_note_is_not_logged(self):
        drafter, _ = gemini(exc=TimeoutError("read timed out"))
        with self.assertLogs("crewlink.ai", level="WARNING") as logs:
            with self.assertRaises(ai.AIUnavailable):
                drafter.draft(NOTE)
        self.assertIn("TimeoutError", logs.output[0])
        self.assertNotIn("contractor", " ".join(logs.output))  # note content stays private

    def test_invalid_json_becomes_ai_unavailable(self):
        drafter, _ = gemini(text="Sure! Here's your announcement: ...")
        with self.assertLogs("crewlink.ai", level="WARNING"), self.assertRaises(ai.AIUnavailable):
            drafter.draft(NOTE)

    def test_missing_field_becomes_ai_unavailable(self):
        drafter, _ = gemini(text=json.dumps({"title": "Hi", "body": "There"}))
        with self.assertRaises(ai.AIUnavailable):
            drafter.draft(NOTE)

    def test_empty_response_becomes_ai_unavailable(self):
        drafter, _ = gemini(text=None)  # e.g. blocked by a safety filter
        with self.assertLogs("crewlink.ai", level="WARNING"), self.assertRaises(ai.AIUnavailable):
            drafter.draft(NOTE)

    def test_overlong_preview_is_cut_at_a_word_boundary(self):
        drafter, _ = gemini(text=json.dumps({
            "title": "T", "body": "B", "push_preview": "word " * 60}))
        preview = drafter.draft(NOTE)["push_preview"]
        self.assertLessEqual(len(preview), 120)
        self.assertTrue(preview.endswith("word…"))

    def test_real_client_is_built_with_a_hard_timeout_and_no_retries(self):
        with mock.patch("google.genai.Client") as client_cls:
            ai.GeminiDrafter(api_key="k", model="m", timeout_s=10)
        options = client_cls.call_args.kwargs["http_options"]
        self.assertEqual(options.timeout, 10_000)          # milliseconds
        self.assertEqual(options.retry_options.attempts, 1)

    @override_settings(GEMINI_API_KEY="")
    def test_no_key_means_fake_provider(self):
        self.assertIsInstance(ai.get_drafter(), ai.FakeDrafter)

    def test_fake_provider_tidies_the_brief_example(self):
        out = ai.FakeDrafter().draft(NOTE)
        self.assertIn("meeting Thursday", out["title"])
        self.assertNotIn("EVERYONE", out["body"])
        self.assertLessEqual(len(out["push_preview"]), 120)


# --------------------------------------------------------------- endpoint tests

@override_settings(CACHES=LOCMEM, GEMINI_API_KEY="")
class DraftFromNoteEndpointTests(TestCase):
    URL = "/api/announcements/draft-from-note"

    @classmethod
    def setUpTestData(cls):
        cls.l27 = Local.objects.create(name="Local 27")
        cls.m = Member.objects.create(local=cls.l27, full_name="ana", email="a@x.com",
                                      classification="Journeyman Wireman")
        user = User.objects.create_user("leader27", password="pass12345")
        cls.leader = Account.objects.create(user=user, local=cls.l27, role="leader")
        cls.token = Token.objects.create(user=user).key
        muser = User.objects.create_user("member27", password="pass12345")
        Account.objects.create(user=muser, local=cls.l27, role="member", member=cls.m)
        cls.member_token = Token.objects.create(user=muser).key

    def setUp(self):
        cache.clear()  # throttle counters
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token}")

    def post(self, **data):
        return self.client.post(self.URL, {"note": NOTE, **data}, format="json")

    def test_creates_a_draft_marked_as_ai_generated(self):
        r = self.post()
        self.assertEqual(r.status_code, 201)
        self.assertEqual((r.data["status"], r.data["version"], r.data["ai_generated"],
                          r.data["ai_provider"]), ("draft", 1, True, "fake"))
        ann = Announcement.objects.get(pk=r.data["id"])
        self.assertEqual((ann.local_id, ann.created_by_id), (self.l27.id, self.leader.id))

    def test_ai_draft_cannot_be_sent_until_a_human_approves_it(self):
        ann_id = self.post().data["id"]
        self.assertEqual(self.client.post(f"/api/announcements/{ann_id}/send").status_code, 409)
        self.client.post(f"/api/announcements/{ann_id}/approve", {"version": 1}, format="json")
        with mock.patch("announcements.tasks.enqueue"), self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.post(f"/api/announcements/{ann_id}/send").status_code, 202)

    def test_editing_the_ai_text_requires_approving_again(self):
        ann_id = self.post().data["id"]
        self.client.patch(f"/api/announcements/{ann_id}", {"body": "Changed"}, format="json")
        r = self.client.post(f"/api/announcements/{ann_id}/approve", {"version": 1}, format="json")
        self.assertEqual(r.status_code, 409)

    def test_provider_down_returns_503_and_saves_nothing(self):
        broken, _ = gemini(exc=ConnectionError("unreachable"))
        with mock.patch.object(ai, "get_drafter", return_value=broken), \
             self.assertLogs("crewlink.ai", level="WARNING"):
            r = self.post()
        self.assertEqual((r.status_code, r.data["code"]), (503, "ai_unavailable"))
        self.assertFalse(Announcement.objects.exists())

    def test_invalid_options_fail_before_calling_the_provider(self):
        spy = mock.Mock(wraps=ai.FakeDrafter())
        with mock.patch.object(ai, "get_drafter", return_value=spy):
            typo = self.post(audience_classification="Apprentce")
            past = self.post(kind="meeting", event_at=(timezone.now() - timedelta(hours=1)).isoformat())
        self.assertEqual((typo.status_code, past.status_code), (400, 400))
        spy.draft.assert_not_called()

    def test_overlong_note_is_rejected_without_calling_the_provider(self):
        spy = mock.Mock(wraps=ai.FakeDrafter())
        with mock.patch.object(ai, "get_drafter", return_value=spy):
            r = self.client.post(self.URL, {"note": "x" * 2001}, format="json")
        self.assertEqual(r.status_code, 400)
        spy.draft.assert_not_called()

    def test_leader_options_go_on_the_draft(self):
        when = (timezone.now() + timedelta(days=2)).isoformat()
        r = self.post(kind="meeting", event_at=when, needs_ack=True)
        self.assertEqual((r.data["kind"], r.data["needs_ack"]), ("meeting", True))

    def test_member_gets_403_and_anonymous_gets_401(self):
        member = APIClient()
        member.credentials(HTTP_AUTHORIZATION=f"Token {self.member_token}")
        self.assertEqual(member.post(self.URL, {"note": NOTE}, format="json").status_code, 403)
        self.assertEqual(APIClient().post(self.URL, {"note": NOTE}, format="json").status_code, 401)

    def test_rate_limited_per_leader(self):
        with mock.patch.object(AIDraftThrottle, "THROTTLE_RATES", {"ai_draft": "2/min"}):
            codes = [self.post().status_code for _ in range(3)]
        self.assertEqual(codes, [201, 201, 429])

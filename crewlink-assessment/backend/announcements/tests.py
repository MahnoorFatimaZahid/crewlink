"""
Model-level guarantees. These tests go *around* Django's Python validation on purpose:
the point is to prove Postgres itself refuses bad data, even from a buggy worker.
"""
from django.contrib.auth.models import User
from django.db import DataError, IntegrityError, transaction
from django.test import TestCase

from announcements.models import Announcement, AnnouncementRecipient
from core.models import Account, Local, Member


def make_world():
    local = Local.objects.create(name="Local T")
    member = Member.objects.create(local=local, full_name="A", email="a@x.example",
                                   classification="Foreman")
    user = User.objects.create_user("lead", password="x")
    leader = Account.objects.create(user=user, local=local, role="leader")
    ann = Announcement.objects.create(local=local, title="t", body="b", push_preview="p",
                                      created_by=leader)
    return local, member, leader, ann


class RecipientUniquenessTests(TestCase):
    """Rule 2's foundation: one row per member per announcement, enforced by Postgres."""

    def test_second_row_for_same_member_is_refused(self):
        local, member, _, ann = make_world()
        AnnouncementRecipient.objects.create(announcement=ann, member=member, local=local)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AnnouncementRecipient.objects.create(announcement=ann, member=member, local=local)

    def test_rerunning_a_fan_out_inserts_nothing_new(self):
        # What a restarted fan-out job does: insert everyone again, ignoring conflicts.
        local, member, _, ann = make_world()
        row = lambda: AnnouncementRecipient(announcement=ann, member=member, local=local)
        AnnouncementRecipient.objects.bulk_create([row()], ignore_conflicts=True)
        AnnouncementRecipient.objects.bulk_create([row()], ignore_conflicts=True)
        self.assertEqual(ann.recipients.count(), 1)


class AnnouncementConstraintTests(TestCase):
    def test_cannot_leave_draft_without_a_named_approver(self):
        # Even a buggy code path can't mark AI text as sendable without a human.
        _, _, _, ann = make_world()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Announcement.objects.filter(pk=ann.pk).update(status="approved")

    def test_push_preview_over_120_chars_is_refused_by_db(self):
        local, _, leader, _ = make_world()
        with self.assertRaises(DataError), transaction.atomic():
            Announcement.objects.create(local=local, title="t", body="b",
                                        push_preview="x" * 121, created_by=leader)

    def test_meeting_needs_an_event_time(self):
        local, _, leader, _ = make_world()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Announcement.objects.create(local=local, title="t", body="b", push_preview="p",
                                        kind="meeting", created_by=leader)

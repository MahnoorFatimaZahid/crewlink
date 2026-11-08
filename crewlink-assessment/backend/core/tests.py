import io
import json

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase

from announcements.models import Announcement, AnnouncementRecipient
from core.models import Account, Local, Member


class AccountRulesTests(TestCase):
    """Rule 1 starts here: an account's local and role must be trustworthy."""

    def setUp(self):
        self.l27 = Local.objects.create(name="Local 27")
        self.l12 = Local.objects.create(name="Local 12")
        self.m12 = Member.objects.create(local=self.l12, full_name="B", email="b@x.example",
                                         classification="Foreman")

    def test_member_login_must_point_at_a_roster_row_db_level(self):
        user = User.objects.create_user("u", password="x")
        # bulk_create skips save()/full_clean(), so this tests the Postgres CHECK itself.
        with self.assertRaises(IntegrityError), transaction.atomic():
            Account.objects.bulk_create([Account(user=user, local=self.l27, role="member")])

    def test_login_cannot_point_at_another_locals_member(self):
        user = User.objects.create_user("u", password="x")
        with self.assertRaises(ValidationError):
            Account.objects.create(user=user, local=self.l27, role="member", member=self.m12)

    def test_cross_local_member_login_refused_by_db_on_insert(self):
        # bulk_create skips clean(), so only the composite FK can stop this.
        user = User.objects.create_user("u", password="x")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Account.objects.bulk_create(
                [Account(user=user, local=self.l27, role="member", member=self.m12)])

    def test_cross_local_member_login_refused_by_db_on_update(self):
        # A valid account, then a queryset .update() moves it to another local.
        user = User.objects.create_user("u", password="x")
        acct = Account.objects.create(user=user, local=self.l12, role="member", member=self.m12)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Account.objects.filter(pk=acct.pk).update(local=self.l27)

    def test_leader_without_member_is_unaffected_by_composite_fk(self):
        user = User.objects.create_user("u", password="x")
        Account.objects.bulk_create([Account(user=user, local=self.l27, role="leader")])
        self.assertTrue(Account.objects.filter(user=user, member__isnull=True).exists())


class SeedTests(TestCase):
    def run_seed(self):
        buf = io.StringIO()
        call_command("seed", stdout=buf)
        return json.loads(buf.getvalue())

    def test_seed_is_rerunnable_with_stable_ids(self):
        first = self.run_seed()
        second = self.run_seed()
        self.assertEqual(first, second)  # same ids -> README Test Accounts stay valid
        self.assertEqual(Local.objects.count(), 2)
        self.assertEqual(Member.objects.count(), 2200)
        self.assertEqual(Announcement.objects.count(), 1)

    def test_seeded_announcement_only_reached_active_members_of_its_local(self):
        out = self.run_seed()
        ann = Announcement.objects.get(pk=out["Local 27"]["sent_announcement_id"])
        recipients = AnnouncementRecipient.objects.filter(announcement=ann)
        active_27 = Member.objects.filter(local=ann.local, status="active").count()
        self.assertEqual(recipients.count(), active_27)
        self.assertFalse(recipients.exclude(member__local=ann.local).exists())
        self.assertFalse(recipients.exclude(member__status="active").exists())
        self.assertFalse(recipients.exclude(local=ann.local).exists())

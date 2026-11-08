"""
Rule 2: a retry or a restart must never deliver twice to the same member.

These tests call the sending functions directly (no Redis, no Celery worker),
and simulate the failures we designed for: double clicks, crashed workers,
lost jobs, flaky push providers, and real concurrent threads.
"""
import threading
from collections import Counter
from datetime import timedelta
from io import StringIO
from unittest import mock, skipUnless

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from announcements import sending, tasks
from announcements.integrity import find_problems
from announcements.models import Announcement, AnnouncementRecipient, PushLog
from announcements.push import LoggingPushSender
from core.models import Account, Local, Member

User = get_user_model()
S = Announcement.Status
P = AnnouncementRecipient.PushStatus
LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
ON_POSTGRES = connection.vendor == "postgresql"


# ----------------------------------------------------------------- helpers

class OkSender:
    def send(self, ann, rows):
        return {r.id: (True, "") for r in rows}


class FlakySender:
    """Fails the first `fail_times` attempts for the given members, then succeeds."""

    def __init__(self, member_ids, fail_times=1):
        self.left = Counter({m: fail_times for m in member_ids})

    def send(self, ann, rows):
        out = {}
        for r in rows:
            if self.left[r.member_id] > 0:
                self.left[r.member_id] -= 1
                out[r.id] = (False, "provider error")
            else:
                out[r.id] = (True, "")
        return out


class CrashingSender:
    """Simulates the worker process dying mid-push."""

    def send(self, ann, rows):
        raise RuntimeError("worker killed")


def build_world(test):
    test.l27 = Local.objects.create(name="Local 27")
    test.l12 = Local.objects.create(name="Local 12")

    def m(local, name, cls="Journeyman Wireman", status="active"):
        return Member.objects.create(local=local, full_name=name, email=f"{name}@x.com",
                                     classification=cls, status=status)

    test.wiremen = [m(test.l27, f"w{i}") for i in range(5)]
    test.apprentices = [m(test.l27, f"a{i}", cls="Apprentice 3rd Year") for i in range(3)]
    test.retired = m(test.l27, "old", status="retired")
    test.suspended = m(test.l27, "sus", status="suspended")
    test.other_local = [m(test.l12, f"o{i}") for i in range(2)]
    test.active27 = test.wiremen + test.apprentices

    user = User.objects.create_user("leader27", password="pass12345")
    test.leader = Account.objects.create(user=user, local=test.l27, role="leader")
    test.token = Token.objects.create(user=user).key


def approved(test, **extra):
    fields = dict(local=test.l27, title="Emergency meeting", body="Thu 6pm",
                  push_preview="Emergency meeting Thu 6pm", status=S.APPROVED,
                  created_by=test.leader, approved_by=test.leader, approved_at=timezone.now())
    fields.update(extra)
    return Announcement.objects.create(**fields)


def queued(test, **extra):
    ann = approved(test, **extra)
    assert sending.request_send(ann.pk, test.l27.id)
    return ann


def drain(ann, sender=None, batch_size=3, now=None):
    """Run dispatch until nothing is claimable, like a worker chain would."""
    sender = sender or OkSender()
    while sending.dispatch_batch(ann.pk, sender, batch_size=batch_size, now=now):
        pass
    sending.finish_if_done(ann.pk)


def successful_pushes_per_member(ann):
    return Counter(PushLog.objects.filter(announcement=ann, ok=True)
                   .values_list("member_id", flat=True))


# ------------------------------------------------------------ guard 1: Send

@override_settings(CACHES=LOCMEM)
class SendEndpointTests(TestCase):
    def setUp(self):
        build_world(self)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token}")

    def send(self, ann):
        return self.client.post(f"/api/announcements/{ann.pk}/send")

    def test_send_returns_202_and_queues_fan_out_once_even_if_clicked_twice(self):
        ann = approved(self)
        with mock.patch.object(tasks, "enqueue") as enqueue:
            with self.captureOnCommitCallbacks(execute=True):
                first = self.send(ann)
            with self.captureOnCommitCallbacks(execute=True):
                second = self.send(ann)
        self.assertEqual((first.status_code, second.status_code), (202, 202))
        self.assertEqual(second.data["status"], "queued")
        enqueue.assert_called_once_with(tasks.fan_out_task, ann.pk)

    def test_job_is_only_queued_after_commit(self):
        ann = approved(self)
        with mock.patch.object(tasks, "enqueue") as enqueue:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                self.send(ann)
            enqueue.assert_not_called()  # nothing queued while the transaction is open
            self.assertEqual(len(callbacks), 1)

    def test_draft_cannot_be_sent(self):
        ann = approved(self, status=S.DRAFT, approved_by=None, approved_at=None)
        self.assertEqual(self.send(ann).status_code, 409)
        ann.refresh_from_db()
        self.assertEqual(ann.status, S.DRAFT)

    def test_meeting_already_over_cannot_be_sent(self):
        ann = approved(self, kind="meeting", event_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.send(ann).status_code, 409)

    def test_send_still_succeeds_when_redis_is_down(self):
        # The status change is committed; the lost job is recovered by the sweeper.
        ann = approved(self)
        with override_settings(REDIS_URL="redis://unreachable:6379/0"), \
             mock.patch.object(tasks.fan_out_task, "apply_async", side_effect=ConnectionError), \
             self.assertLogs("crewlink.sending", level="ERROR"):
            with self.captureOnCommitCallbacks(execute=True):
                r = self.send(ann)
        self.assertEqual(r.status_code, 202)
        ann.refresh_from_db()
        self.assertEqual(ann.status, S.QUEUED)
        later = timezone.now() + sending.STUCK_QUEUED + timedelta(seconds=1)
        self.assertIn(ann.pk, sending.sweep(now=later)["fan_out"])


# ------------------------------------------------------------ guard 2: fan-out

class FanOutTests(TestCase):
    def setUp(self):
        build_world(self)

    def recipient_members(self, ann):
        return set(ann.recipients.values_list("member_id", flat=True))

    def test_audience_is_active_members_of_the_local_only(self):
        ann = queued(self)
        sending.fan_out(ann.pk)
        self.assertEqual(self.recipient_members(ann), {m.id for m in self.active27})
        ann.refresh_from_db()
        self.assertEqual(ann.status, S.SENDING)

    def test_classification_narrows_the_audience(self):
        ann = queued(self, audience_classification="Apprentice 3rd Year")
        sending.fan_out(ann.pk)
        self.assertEqual(self.recipient_members(ann), {m.id for m in self.apprentices})

    def test_running_fan_out_again_inserts_nothing(self):
        ann = queued(self)
        self.assertEqual(sending.fan_out(ann.pk), 8)
        self.assertEqual(sending.fan_out(ann.pk), 0)
        self.assertEqual(ann.recipients.count(), 8)

    def test_crash_mid_fan_out_leaves_no_rows_then_rerun_completes(self):
        # Crash after the INSERT but before commit: the whole transaction rolls back.
        ann = queued(self)
        with mock.patch.object(Announcement, "save", side_effect=RuntimeError("worker killed")):
            with self.assertRaises(RuntimeError):
                sending.fan_out(ann.pk)
        ann.refresh_from_db()
        self.assertEqual((ann.status, ann.recipients.count()), (S.QUEUED, 0))
        self.assertEqual(sending.fan_out(ann.pk), 8)  # the retried job does it all

    def test_on_conflict_still_blocks_duplicates_as_a_second_line_of_defence(self):
        ann = queued(self)
        sending.fan_out(ann.pk)
        # Force the status back, as if guard 1 had a bug, and run fan-out again.
        Announcement.objects.filter(pk=ann.pk).update(status=S.QUEUED)
        self.assertEqual(sending.fan_out(ann.pk), 0)
        self.assertEqual(ann.recipients.count(), 8)

    def test_fan_out_does_nothing_for_an_unsent_announcement(self):
        ann = approved(self)
        self.assertEqual(sending.fan_out(ann.pk), 0)
        self.assertEqual(ann.recipients.count(), 0)

    def test_audience_is_fixed_at_send_time(self):
        ann = queued(self)
        sending.fan_out(ann.pk)
        Member.objects.create(local=self.l27, full_name="newbie", email="n@x.com",
                              classification="Journeyman Wireman")
        sending.fan_out(ann.pk)  # e.g. the sweeper re-runs it later
        self.assertEqual(ann.recipients.count(), 8)


# ----------------------------------------------------------- guard 3: dispatch

class DispatchTests(TestCase):
    def setUp(self):
        build_world(self)
        self.ann = queued(self)
        sending.fan_out(self.ann.pk)

    def test_every_member_gets_exactly_one_push_across_many_batches(self):
        drain(self.ann, batch_size=3)  # 8 rows -> 3 batches
        pushes = successful_pushes_per_member(self.ann)
        self.assertEqual(set(pushes), {m.id for m in self.active27})
        self.assertEqual(set(pushes.values()), {1})
        self.ann.refresh_from_db()
        self.assertEqual(self.ann.status, S.SENT)

    def test_running_dispatch_again_after_completion_pushes_nothing(self):
        drain(self.ann)
        drain(self.ann)  # e.g. a duplicate job from the sweeper
        self.assertEqual(PushLog.objects.filter(announcement=self.ann).count(), 8)

    def test_worker_crash_mid_push_recovers_and_is_detected(self):
        with self.assertRaises(RuntimeError):
            sending.dispatch_batch(self.ann.pk, CrashingSender(), batch_size=3)
        self.assertEqual(self.ann.recipients.filter(push_status=P.SENDING).count(), 3)
        # The intent log survived the crash: 3 members MAY have been pushed.
        self.assertEqual(PushLog.objects.filter(announcement=self.ann, ok__isnull=True).count(), 3)

        # Nothing happens until the rows count as stuck...
        self.assertEqual(sending.sweep()["reset"], 0)
        # ...then the sweeper resets them and a new worker finishes the job.
        later = timezone.now() + sending.STUCK_SENDING + timedelta(seconds=1)
        with self.assertLogs("crewlink.sending", level="WARNING"):
            self.assertEqual(sending.sweep(now=later)["reset"], 3)
        drain(self.ann, now=later)
        self.assertEqual(set(successful_pushes_per_member(self.ann).values()), {1})
        self.assertEqual(self.ann.recipients.filter(push_status=P.SENT).count(), 8)

        # The honest gap is visible, not hidden: those 3 may have been pushed twice.
        self.assertIn("RULE2: 3 recipients received, or may have received, "
                      "more than one push for one callout", find_problems())

    def test_push_log_is_written_before_the_provider_is_called(self):
        seen = {}

        class SpySender(OkSender):
            def send(inner, ann, rows):
                seen["logged"] = PushLog.objects.filter(announcement=ann, ok__isnull=True).count()
                return super().send(ann, rows)

        sending.dispatch_batch(self.ann.pk, SpySender(), batch_size=3)
        self.assertEqual(seen["logged"], 3)  # logged first...
        self.assertEqual(PushLog.objects.filter(announcement=self.ann, ok=True).count(), 3)  # ...then filled in

    def test_failed_push_waits_then_retries_once(self):
        flaky_member = self.wiremen[0].id
        sender = FlakySender([flaky_member], fail_times=1)
        now = timezone.now()
        drain(self.ann, sender, now=now)
        row = self.ann.recipients.get(member_id=flaky_member)
        self.assertEqual((row.push_status, row.push_attempts), (P.PENDING, 1))

        drain(self.ann, sender, now=now + timedelta(seconds=5))  # too soon: not retried
        self.assertEqual(self.ann.recipients.get(member_id=flaky_member).push_attempts, 1)

        drain(self.ann, sender, now=now + sending.RETRY_AFTER)
        row.refresh_from_db()
        self.assertEqual((row.push_status, row.push_attempts), (P.SENT, 2))
        self.assertEqual(successful_pushes_per_member(self.ann)[flaky_member], 1)

    def test_gives_up_after_max_attempts_but_message_stays_in_inbox(self):
        member = self.wiremen[0].id
        sender = FlakySender([member], fail_times=99)
        now = timezone.now()
        for i in range(sending.MAX_ATTEMPTS + 2):
            drain(self.ann, sender, now=now + i * sending.RETRY_AFTER)
        row = self.ann.recipients.get(member_id=member)
        self.assertEqual((row.push_status, row.push_attempts), (P.FAILED, sending.MAX_ATTEMPTS))
        self.ann.refresh_from_db()
        self.assertEqual(self.ann.status, S.SENT)  # failed is final; the send is complete

    def test_meeting_that_ends_mid_send_is_not_pushed(self):
        Announcement.objects.filter(pk=self.ann.pk).update(
            kind="meeting", event_at=timezone.now() - timedelta(minutes=1))
        drain(self.ann)
        self.assertEqual(self.ann.recipients.filter(push_status=P.SKIPPED).count(), 8)
        self.assertFalse(PushLog.objects.filter(announcement=self.ann).exists())

    def test_member_suspended_after_send_is_skipped(self):
        Member.objects.filter(pk=self.wiremen[0].pk).update(status="suspended")
        drain(self.ann)
        self.assertEqual(self.ann.recipients.get(member=self.wiremen[0]).push_status, P.SKIPPED)
        self.assertNotIn(self.wiremen[0].id, successful_pushes_per_member(self.ann))

    def test_late_result_does_not_overwrite_a_sweeper_reset(self):
        """A slow worker finishes after the sweeper already reset its rows."""
        sweeper_ran = {}

        class SlowSender(OkSender):
            def send(inner, ann, rows):
                later = timezone.now() + sending.STUCK_SENDING + timedelta(seconds=1)
                sweeper_ran["reset"] = sending.sweep(now=later)["reset"]
                return super().send(ann, rows)

        with self.assertLogs("crewlink.sending", level="WARNING"):
            sending.dispatch_batch(self.ann.pk, SlowSender(), batch_size=8)
        self.assertEqual(sweeper_ran["reset"], 8)
        # The slow worker's "sent" didn't overwrite the reset: rows are pending again.
        self.assertEqual(self.ann.recipients.filter(push_status=P.PENDING).count(), 8)


# --------------------------------------------------------------------- sweeper

class SweeperTests(TestCase):
    def setUp(self):
        build_world(self)

    def test_recent_queued_announcement_is_left_alone(self):
        ann = queued(self)
        self.assertNotIn(ann.pk, sending.sweep()["fan_out"])

    def test_stalled_dispatch_is_restarted_but_active_one_is_not(self):
        ann = queued(self)
        sending.fan_out(ann.pk)
        self.assertIn(ann.pk, sending.sweep()["dispatch"])  # pending, never claimed

        sending.dispatch_batch(ann.pk, OkSender(), batch_size=2)  # a worker is busy on it
        self.assertNotIn(ann.pk, sending.sweep()["dispatch"])

    def test_lost_final_step_is_finished_by_the_sweeper(self):
        ann = queued(self)
        sending.fan_out(ann.pk)
        while sending.dispatch_batch(ann.pk, OkSender()):
            pass  # all pushed, but "mark sent" never ran
        self.assertEqual(sending.sweep()["finished"], 1)
        ann.refresh_from_db()
        self.assertEqual(ann.status, S.SENT)

    def test_sweep_task_requeues_lost_jobs(self):
        ann = queued(self)
        Announcement.objects.filter(pk=ann.pk).update(sent_at=timezone.now() - timedelta(minutes=5))
        with mock.patch.object(tasks, "enqueue") as enqueue:
            tasks.sweep_task()
        enqueue.assert_any_call(tasks.fan_out_task, ann.pk)


# ------------------------------------------------------- real concurrency

@skipUnless(ON_POSTGRES, "needs real row locks (Postgres)")
class ConcurrencyTests(TransactionTestCase):
    """Real threads with their own database connections, started at the same instant."""

    def setUp(self):
        build_world(self)

    def run_together(self, n, fn):
        barrier = threading.Barrier(n)
        results, errors = [], []

        def worker():
            try:
                barrier.wait()
                results.append(fn())
            except Exception as e:  # surface thread errors in the test
                errors.append(e)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        return results

    def test_ten_simultaneous_sends_only_one_wins(self):
        ann = approved(self)
        results = self.run_together(10, lambda: sending.request_send(ann.pk, self.l27.id))
        self.assertEqual(results.count(True), 1)

    def test_parallel_fan_outs_create_each_row_once(self):
        ann = queued(self)
        self.run_together(4, lambda: sending.fan_out(ann.pk))
        self.assertEqual(ann.recipients.count(), 8)

    def test_parallel_dispatch_workers_never_push_the_same_member_twice(self):
        # Make a bigger audience so workers really overlap.
        Member.objects.bulk_create([
            Member(local=self.l27, full_name=f"bulk{i}", email=f"b{i}@x.com",
                   classification="Journeyman Wireman") for i in range(300)])
        ann = queued(self)
        sending.fan_out(ann.pk)

        def worker():
            claimed = 0
            while n := sending.dispatch_batch(ann.pk, LoggingPushSender(), batch_size=7):
                claimed += n
            return claimed

        claimed = self.run_together(4, worker)
        self.assertEqual(sum(claimed), 308)               # every row claimed exactly once
        self.assertGreater(min(claimed), 0)               # and work really was shared
        self.assertEqual(set(successful_pushes_per_member(ann).values()), {1})
        self.assertEqual(len(successful_pushes_per_member(ann)), 308)


# ------------------------------------------------------------------- stats

@override_settings(CACHES=LOCMEM)
class StatsTests(TestCase):
    def setUp(self):
        build_world(self)
        cache.clear()
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token}")
        self.ann = queued(self, needs_ack=True)
        sending.fan_out(self.ann.pk)
        drain(self.ann)
        rows = list(self.ann.recipients.all())
        now = timezone.now()
        AnnouncementRecipient.objects.filter(pk__in=[r.pk for r in rows[:5]]).update(
            received_at=now, read_at=now)
        AnnouncementRecipient.objects.filter(pk__in=[r.pk for r in rows[:2]]).update(acked_at=now)

    def url(self, ann=None):
        return f"/api/announcements/{(ann or self.ann).pk}/stats"

    def test_counts_are_right(self):
        c = self.client.get(self.url()).data["counts"]
        self.assertEqual((c["total"], c["sent"], c["read"], c["acknowledged"], c["not_seen"]),
                         (8, 8, 5, 2, 3))

    def test_second_call_within_ttl_does_not_count_again(self):
        self.client.get(self.url())
        with CaptureQueriesContext(connection) as q:
            self.client.get(self.url())
        self.assertFalse([s for s in q.captured_queries if "COUNT(" in s["sql"].upper()])

    def test_other_locals_leader_gets_404_even_when_cached(self):
        self.client.get(self.url())  # Local 27's answer is now cached
        user = User.objects.create_user("leader12", password="pass12345")
        Account.objects.create(user=user, local=self.l12, role="leader")
        other = APIClient()
        other.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=user).key}")
        self.assertEqual(other.get(self.url()).status_code, 404)


# --------------------------------------------------------------- integrity

class IntegrityTests(TestCase):
    def setUp(self):
        build_world(self)
        self.ann = queued(self)
        sending.fan_out(self.ann.pk)
        drain(self.ann)

    def test_clean_data_has_no_problems(self):
        self.assertEqual(find_problems(), [])
        out = StringIO()
        call_command("check_integrity", stdout=out)
        self.assertIn("passed", out.getvalue())

    def test_a_duplicate_push_is_detected_and_fails_the_command(self):
        row = self.ann.recipients.first()
        PushLog.objects.create(recipient=row, announcement=self.ann, member=row.member, ok=True)
        self.assertTrue(any(p.startswith("RULE2") for p in find_problems()))
        with self.assertRaises(CommandError):
            call_command("check_integrity", stderr=StringIO())

    def test_rows_stuck_in_sending_are_reported(self):
        self.ann.recipients.filter(pk=self.ann.recipients.first().pk).update(
            push_status=P.SENDING, claimed_at=timezone.now() - timedelta(hours=1))
        self.assertTrue(any(p.startswith("HEALTH") for p in find_problems()))


# ------------------------------------------------------------ process_sends

class ProcessSendsTests(TestCase):
    """The no-Redis local worker reaches the same end state as Celery would."""

    def test_one_pass_sends_everything_once(self):
        build_world(self)
        ann = queued(self)
        call_command("process_sends", "--once", stdout=StringIO())
        ann.refresh_from_db()
        self.assertEqual(ann.status, S.SENT)
        self.assertEqual(set(successful_pushes_per_member(ann).values()), {1})
        call_command("process_sends", "--once", stdout=StringIO())  # running again is harmless
        self.assertEqual(PushLog.objects.filter(announcement=ann).count(), 8)

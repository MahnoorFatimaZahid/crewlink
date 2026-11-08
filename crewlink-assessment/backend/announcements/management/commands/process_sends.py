"""
Local worker for running WITHOUT Redis or Celery (e.g. on Windows without Docker).

    python manage.py process_sends          # keeps running, like a worker
    python manage.py process_sends --once   # one pass, then exit

It works because every bit of state lives in Postgres: it just looks for queued and
sending announcements and runs the same idempotent functions the Celery tasks use.
Redis speeds the system up; it isn't needed for correctness.

Safe to run next to Celery too: claims use row locks, so nothing is pushed twice.
"""
import time

from django.core.management.base import BaseCommand

from announcements import sending
from announcements.models import Announcement
from announcements.push import get_sender

S = Announcement.Status


class Command(BaseCommand):
    help = "Process queued/sending announcements by polling Postgres (no Redis needed)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run one pass and exit.")
        parser.add_argument("--interval", type=float, default=1.0, help="Seconds between passes.")

    def handle(self, *args, once=False, interval=1.0, **opts):
        sender = get_sender()
        self.stdout.write("process_sends: polling Postgres for work (Ctrl+C to stop)")
        while True:
            self.one_pass(sender)
            if once:
                return
            time.sleep(interval)

    def one_pass(self, sender):
        sending.sweep()  # resets rows whose worker died, marks finished sends
        for ann_id in Announcement.objects.filter(status=S.QUEUED).values_list("id", flat=True):
            sending.fan_out(ann_id)
        for ann_id in Announcement.objects.filter(status=S.SENDING).values_list("id", flat=True):
            pushed = 0
            while claimed := sending.dispatch_batch(ann_id, sender):
                pushed += claimed
            if sending.finish_if_done(ann_id) or pushed:
                self.stdout.write(f"announcement {ann_id}: processed {pushed} rows")

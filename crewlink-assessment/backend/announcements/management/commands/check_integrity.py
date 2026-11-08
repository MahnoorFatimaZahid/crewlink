"""
    python manage.py check_integrity

Prints any broken invariant and exits with code 1, so a nightly cron job or CI step
can alert on it. Exit code 0 means everything checked out.
"""
from django.core.management.base import BaseCommand, CommandError

from announcements.integrity import find_problems


class Command(BaseCommand):
    help = "Check Rule 1 and Rule 2 invariants. Exit code 1 if anything is broken."

    def handle(self, *args, **opts):
        problems = find_problems()
        if problems:
            for p in problems:
                self.stderr.write(p)
            raise CommandError(f"{len(problems)} integrity problem(s) found")
        self.stdout.write(self.style.SUCCESS("All integrity checks passed."))

"""
Detection (DESIGN.md, "Finding out in production that a rule already broke").

Each check is an invariant: a query that must return nothing. Prevention should make
every one of these impossible; this is the alarm for when prevention failed anyway.
Run nightly with `python manage.py check_integrity` (exit code 1 = alert).
"""
from datetime import timedelta

from django.db.models import Count, F, Q
from django.utils import timezone

from core.models import Account

from .models import AnnouncementRecipient, PushLog


def find_problems(now=None):
    now = now or timezone.now()
    problems = []

    # Rule 1: a recipient row in a different local than its announcement or member.
    # (The composite foreign keys should make these impossible. Check anyway.)
    n = AnnouncementRecipient.objects.exclude(local_id=F("announcement__local_id")).count()
    if n:
        problems.append(f"RULE1: {n} recipient rows in a different local than their announcement")
    n = AnnouncementRecipient.objects.exclude(local_id=F("member__local_id")).count()
    if n:
        problems.append(f"RULE1: {n} recipient rows in a different local than their member")

    # Rule 1: a member login linked to another local's member.
    n = (Account.objects.filter(role=Account.Role.MEMBER)
         .exclude(local_id=F("member__local_id")).count())
    if n:
        problems.append(f"RULE1: {n} member logins linked to another local's member")

    # Rule 2: a member who got (or may have got) more than one push for one callout.
    # ok=None counts too: it means the push may have gone out before a crash.
    maybe_sent = PushLog.objects.filter(Q(ok=True) | Q(ok__isnull=True))
    dupes = (maybe_sent.values("recipient_id")
             .annotate(n=Count("id")).filter(n__gt=1).count())
    if dupes:
        problems.append(f"RULE2: {dupes} recipients received, or may have received, "
                        f"more than one push for one callout")

    # Health: rows stuck in `sending` far longer than the sweeper allows = sweeper not running.
    n = AnnouncementRecipient.objects.filter(
        push_status=AnnouncementRecipient.PushStatus.SENDING,
        claimed_at__lt=now - timedelta(minutes=15)).count()
    if n:
        problems.append(f"HEALTH: {n} rows stuck in sending for over 15 minutes (is the sweeper running?)")

    return problems

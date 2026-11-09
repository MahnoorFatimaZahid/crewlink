"""
The send path (DESIGN.md, Part A §2) and the Rule 2 guards (§3).

Plain functions, no Celery here: tasks.py wraps them, and tests call them directly.
Every function is idempotent, so running any of them twice, or after a crash, is safe.

  request_send()   guard 1: approved -> queued happens once
  fan_out()        guard 2: one INSERT ... ON CONFLICT DO NOTHING
  dispatch_batch() guard 3: claim rows with FOR UPDATE SKIP LOCKED; push only what we claimed
  finish_if_done() sending -> sent when nothing is left
  sweep()          rebuilds lost work from Postgres state (Redis can lose jobs)
"""
import logging
from datetime import timedelta

from django.db import connection, transaction
from django.db.models import F, Q
from django.utils import timezone

from core.models import Member

from .models import Announcement, AnnouncementRecipient, PushLog

log = logging.getLogger("crewlink.sending")

S = Announcement.Status
P = AnnouncementRecipient.PushStatus

BATCH_SIZE = 500                       # also FCM's per-call multicast limit
PARALLEL_CHAINS = 4                    # dispatch chains started per send
MAX_ATTEMPTS = 3                       # then the row is `failed` (still in the inbox)
RETRY_AFTER = timedelta(seconds=30)    # a failed push waits this long before a retry
STUCK_QUEUED = timedelta(minutes=1)    # queued this long = fan-out job was lost
STUCK_SENDING = timedelta(minutes=5)   # a row claimed this long ago = its worker died
STALLED = timedelta(minutes=2)         # no claims this long = dispatch jobs were lost


# ---------------------------------------------------------------- guard 1: Send

def request_send(announcement_id, local_id, now=None):
    """approved -> queued, in ONE statement. Returns True only for the first caller.

    Two leaders clicking at the same instant: Postgres locks the row for the first
    UPDATE; the second waits, re-checks `status = 'approved'`, matches 0 rows.
    """
    return Announcement.objects.filter(
        pk=announcement_id, local_id=local_id, status=S.APPROVED,
    ).update(status=S.QUEUED, sent_at=now or timezone.now()) == 1


# ------------------------------------------------------------- guard 2: fan-out

def fan_out(announcement_id):
    """Create one recipient row per active member in the audience.

    One statement: the 22,400 member ids never leave Postgres. The insert and the
    switch to `sending` share one transaction, so either all rows exist (`sending`)
    or none do (`queued`). That's why it only runs while `queued`: re-running it
    after it finished must NOT add members who joined after Send (the audience is
    fixed at send time). ON CONFLICT is the second line of defence against
    duplicates. Returns the number of rows inserted this run.
    """
    with transaction.atomic():
        # The row lock makes parallel fan-outs of the same announcement take turns:
        # the second one waits, then sees `sending` and does nothing.
        ann = Announcement.objects.select_for_update().get(pk=announcement_id)
        if ann.status != S.QUEUED:
            return 0
        recipients = AnnouncementRecipient._meta.db_table
        members = Member._meta.db_table
        with connection.cursor() as cur:
            cur.execute(f"""
                INSERT INTO {recipients}
                    (id, announcement_id, member_id, local_id,
                     push_status, push_attempts, created_at)
                SELECT gen_random_uuid(), %s, m.id, m.local_id, %s, 0, now()
                FROM {members} m
                WHERE m.local_id = %s
                  AND m.status = %s
                  AND (%s = '' OR m.classification = %s)
                ON CONFLICT (announcement_id, member_id) DO NOTHING
            """, [ann.id, P.PENDING, ann.local_id, Member.Status.ACTIVE,
                  ann.audience_classification, ann.audience_classification])
            inserted = cur.rowcount
        ann.status = S.SENDING
        ann.save(update_fields=["status"])
    log.info("fan_out announcement=%s inserted=%s", announcement_id, inserted)
    return inserted


# ------------------------------------------------------------ guard 3: dispatch

def dispatch_batch(announcement_id, sender, batch_size=BATCH_SIZE, now=None):
    """Claim up to batch_size rows, push them, record each result.

    Returns how many rows were claimed (0 means nothing is claimable right now).
    """
    now = now or timezone.now()

    # 1. Claim. SKIP LOCKED: parallel workers (even on other instances) each get
    #    different rows instead of waiting. The lock + the `pending` check is what
    #    makes it correct; SKIP LOCKED is what makes it fast.
    with transaction.atomic():
        ids = list(
            AnnouncementRecipient.objects
            .select_for_update(skip_locked=True)
            .filter(announcement_id=announcement_id, push_status=P.PENDING)
            .filter(Q(claimed_at__isnull=True) | Q(claimed_at__lte=now - RETRY_AFTER))
            .order_by("created_at")
            .values_list("id", flat=True)[:batch_size]
        )
        if not ids:
            return 0
        AnnouncementRecipient.objects.filter(id__in=ids).update(
            push_status=P.SENDING, claimed_at=now, push_attempts=F("push_attempts") + 1)
    # Committed: these rows are ours. No one else will push them.

    rows = list(AnnouncementRecipient.objects
                .filter(id__in=ids).select_related("announcement", "member"))
    ann = rows[0].announcement

    # 2. Last checks before pushing: meeting over, or member suspended after Send.
    expired = ann.event_at is not None and ann.event_at <= now
    skip = {r.id for r in rows if expired or r.member.status != Member.Status.ACTIVE}
    to_push = [r for r in rows if r.id not in skip]
    if skip:
        AnnouncementRecipient.objects.filter(id__in=skip, push_status=P.SENDING) \
            .update(push_status=P.SKIPPED)

    # 3. Intent log FIRST (ok=None means "handed to the provider, outcome unknown"),
    #    then push. If the process dies mid-push, the rows stay `sending` (the sweeper
    #    resets them) and these log rows still say exactly who MAY have been pushed,
    #    so check_integrity can see possible duplicates instead of being blind to them.
    logs = {r.id: PushLog(recipient_id=r.id, announcement_id=ann.id, member_id=r.member_id,
                          ok=None, detail="attempting") for r in to_push}
    PushLog.objects.bulk_create(logs.values())
    results = sender.send(ann, to_push) if to_push else {}
    for rid, (good, detail) in results.items():
        logs[rid].ok, logs[rid].detail = good, detail[:200]
    PushLog.objects.bulk_update(logs.values(), ["ok", "detail"])

    # 4. Record results. `push_status=SENDING` in each filter: if the sweeper already
    #    reset a row (we were too slow), we don't overwrite its newer state.
    ok = [rid for rid, (good, _) in results.items() if good]
    bad = [rid for rid, (good, _) in results.items() if not good]
    AnnouncementRecipient.objects.filter(id__in=ok, push_status=P.SENDING) \
        .update(push_status=P.SENT, sent_at=now)
    # Failed with tries left: back to pending; claimed_at keeps the attempt time,
    # so the claim filter above makes it wait RETRY_AFTER.
    AnnouncementRecipient.objects.filter(
        id__in=bad, push_status=P.SENDING, push_attempts__lt=MAX_ATTEMPTS,
    ).update(push_status=P.PENDING)
    AnnouncementRecipient.objects.filter(
        id__in=bad, push_status=P.SENDING, push_attempts__gte=MAX_ATTEMPTS,
    ).update(push_status=P.FAILED)
    return len(ids)


def has_pending(announcement_id):
    return AnnouncementRecipient.objects.filter(
        announcement_id=announcement_id, push_status__in=[P.PENDING, P.SENDING]).exists()


def finish_if_done(announcement_id):
    """sending -> sent once no row is pending or sending. Safe to call any time."""
    if has_pending(announcement_id):
        return False
    Announcement.objects.filter(pk=announcement_id, status=S.SENDING).update(status=S.SENT)
    return True


# --------------------------------------------------------------------- sweeper

def sweep(now=None):
    """Find work that stalled and say what to restart. Reads only Postgres.

    Returns {"reset": n, "fan_out": [ids], "dispatch": [ids], "finished": n}.
    The caller (a Celery task, or process_sends) does the re-queuing.
    """
    now = now or timezone.now()

    # 1. Rows claimed long ago: their worker died. Back to pending.
    #    Each reset is a POSSIBLE duplicate (the push may have gone out), so log it.
    reset = AnnouncementRecipient.objects.filter(
        push_status=P.SENDING, claimed_at__lt=now - STUCK_SENDING,
    ).update(push_status=P.PENDING)
    if reset:
        log.warning("SWEEPER_RESET rows=%s (possible duplicate pushes)", reset)

    # 2. Still queued after a minute: the fan-out job was lost (e.g. Redis restarted).
    fan_out_ids = list(Announcement.objects.filter(
        status=S.QUEUED, sent_at__lt=now - STUCK_QUEUED).values_list("id", flat=True))

    # 3. Sending, has pending rows, but nothing was claimed recently: dispatch jobs lost.
    #    An extra job is harmless: it would just find nothing to claim.
    dispatch_ids = list(
        Announcement.objects.filter(status=S.SENDING, recipients__push_status=P.PENDING)
        .exclude(recipients__claimed_at__gte=now - STALLED)
        .values_list("id", flat=True).distinct())

    # 4. Sending with nothing left: the final "mark sent" step was lost.
    finished = 0
    for ann_id in Announcement.objects.filter(status=S.SENDING).values_list("id", flat=True):
        if ann_id not in dispatch_ids and finish_if_done(ann_id):
            finished += 1

    return {"reset": reset, "fan_out": fan_out_ids, "dispatch": dispatch_ids, "finished": finished}

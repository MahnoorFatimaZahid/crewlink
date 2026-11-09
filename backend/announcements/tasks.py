"""
Celery tasks: thin wrappers around sending.py.

acks_late=True: a task is marked done only AFTER it finishes. If a worker dies
mid-task, Redis hands the task to another worker. That's safe only because every
step in sending.py is idempotent.
"""
import logging

from celery import shared_task
from django.conf import settings

from . import sending
from .push import get_sender

log = logging.getLogger("crewlink.sending")


def enqueue(task, *args, countdown=None):
    """Queue a task, but never fail the caller if Redis is unreachable.

    The work to do is already recorded in Postgres (status `queued` / rows `pending`),
    so a lost enqueue just means the sweeper (or process_sends) picks it up later.
    """
    if not getattr(settings, "REDIS_URL", ""):
        return  # no broker configured: `manage.py process_sends` does the work
    try:
        task.apply_async(args=[str(a) for a in args], countdown=countdown)
    except Exception:  # broker down, network blip...
        log.exception("ENQUEUE_FAILED task=%s args=%s (the sweeper will recover it)",
                      task.name, args)


@shared_task(acks_late=True)
def fan_out_task(announcement_id):
    sending.fan_out(announcement_id)
    for _ in range(sending.PARALLEL_CHAINS):
        enqueue(dispatch_task, announcement_id)


@shared_task(acks_late=True)
def dispatch_task(announcement_id):
    """One batch, then queue the next. Each chain has at most one successor,
    so the number of chains never grows."""
    if sending.dispatch_batch(announcement_id, get_sender()):
        enqueue(dispatch_task, announcement_id)
    elif not sending.finish_if_done(announcement_id):
        # Nothing claimable now, but rows are waiting out their retry delay.
        enqueue(dispatch_task, announcement_id, countdown=sending.RETRY_AFTER.total_seconds())


@shared_task
def sweep_task():
    """Runs every minute (Celery beat). Rebuilds lost jobs from Postgres state."""
    result = sending.sweep()
    for ann_id in result["fan_out"]:
        enqueue(fan_out_task, ann_id)
    for ann_id in result["dispatch"]:
        enqueue(dispatch_task, ann_id)
    return {k: (len(v) if isinstance(v, list) else v) for k, v in result.items()}

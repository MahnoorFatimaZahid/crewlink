"""
The push provider, behind a small interface.

The real system would call FCM/APNs here. The brief says real push isn't needed,
so LoggingPushSender writes each push to the log instead. PUSH_FAIL_RATE lets you
simulate the 5-10% of pushes that silently fail, to watch retries work, and
PUSH_DELAY_MS simulates a slow provider, so you can watch the counts climb.

Every push carries the announcement id as its device-side dedupe key (Android
notification `tag`, iOS `apns-collapse-id`): a repeat of the same callout replaces
the notification already shown; different callouts never replace each other.
"""
import logging
import random
import time

from django.conf import settings

log = logging.getLogger("crewlink.push")


class LoggingPushSender:
    def __init__(self, fail_rate=0.0, delay_ms=0):
        self.fail_rate = fail_rate
        self.delay_ms = delay_ms

    def send(self, announcement, rows):
        """Push to each recipient row. Returns {row_id: (ok, detail)}."""
        results = {}
        for row in rows:
            if self.delay_ms:
                time.sleep(self.delay_ms / 1000)
            if random.random() < self.fail_rate:
                results[row.id] = (False, "simulated provider failure")
                continue
            log.info("PUSH member=%s tag=%s preview=%r",
                     row.member_id, announcement.id, announcement.push_preview)
            results[row.id] = (True, "")
        return results


def get_sender():
    return LoggingPushSender(fail_rate=getattr(settings, "PUSH_FAIL_RATE", 0.0),
                             delay_ms=getattr(settings, "PUSH_DELAY_MS", 0))

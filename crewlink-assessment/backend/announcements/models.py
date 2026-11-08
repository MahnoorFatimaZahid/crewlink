import uuid

from django.db import models

from core.models import Account, Local, Member


class Announcement(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft"
        APPROVED = "approved"
        QUEUED = "queued"      # Send pressed; fan-out job queued
        SENDING = "sending"    # recipient rows exist; dispatch in progress
        SENT = "sent"          # nothing pending or sending

    class Kind(models.TextChoices):
        GENERAL = "general"
        MEETING = "meeting"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    local = models.ForeignKey(Local, on_delete=models.PROTECT, related_name="announcements")
    title = models.CharField(max_length=200)
    body = models.TextField()
    push_preview = models.CharField(max_length=120)  # varchar(120): the DB itself rejects longer
    needs_ack = models.BooleanField(default=False)

    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.GENERAL)
    event_at = models.DateTimeField(null=True, blank=True)  # meetings: used for expiry
    audience_classification = models.CharField(max_length=100, blank=True, default="")  # "" = whole local

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    ai_generated = models.BooleanField(default=False)
    created_by = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="+")
    approved_by = models.ForeignKey(Account, null=True, blank=True,
                                    on_delete=models.PROTECT, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    supersedes = models.ForeignKey("self", null=True, blank=True,
                                   on_delete=models.PROTECT, related_name="corrections")

    sent_at = models.DateTimeField(null=True, blank=True)  # null until Send is pressed
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # "No approval, no send": anything past draft must name a human approver.
            models.CheckConstraint(
                condition=models.Q(status="draft") | models.Q(approved_by__isnull=False),
                name="non_draft_needs_approver",
            ),
            models.CheckConstraint(
                condition=models.Q(kind="general") | models.Q(event_at__isnull=False),
                name="meeting_needs_event_time",
            ),
            models.CheckConstraint(
                condition=~models.Q(push_preview=""),
                name="push_preview_not_empty",
            ),
        ]
        indexes = [models.Index(fields=["local", "-created_at"])]

    def __str__(self):
        return self.title


class AnnouncementRecipient(models.Model):
    """One row per member per callout: the audience snapshot AND the delivery record."""

    class PushStatus(models.TextChoices):
        PENDING = "pending"
        SENDING = "sending"    # claimed by a worker
        SENT = "sent"          # accepted by the push provider (not proof the phone got it)
        FAILED = "failed"      # gave up after retries; still in the member's inbox
        SKIPPED = "skipped"    # meeting already over; no push

    class Rsvp(models.TextChoices):
        COMING = "coming"
        CANT = "cant"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    announcement = models.ForeignKey(Announcement, on_delete=models.CASCADE,
                                     related_name="recipients")
    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="deliveries")
    local = models.ForeignKey(Local, on_delete=models.PROTECT, related_name="+")  # copied in for scoping

    push_status = models.CharField(max_length=10, choices=PushStatus.choices,
                                   default=PushStatus.PENDING)
    push_attempts = models.PositiveSmallIntegerField(default=0)
    claimed_at = models.DateTimeField(null=True, blank=True)  # lets the sweeper find stuck rows

    sent_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    acked_at = models.DateTimeField(null=True, blank=True)
    rsvp = models.CharField(max_length=10, choices=Rsvp.choices, null=True, blank=True)
    rsvp_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # The foundation of Rule 2: Postgres refuses a second row for the same member.
            models.UniqueConstraint(fields=["announcement", "member"],
                                    name="one_recipient_row_per_member"),
        ]
        indexes = [
            models.Index(fields=["announcement", "push_status"]),  # worker claims, counts
            models.Index(fields=["member", "-created_at"]),         # member inbox
        ]


class PushLog(models.Model):
    """Every push attempt, append-only. Lets us *detect* a Rule 2 break in production."""

    recipient = models.ForeignKey(AnnouncementRecipient, on_delete=models.CASCADE,
                                  related_name="push_logs")
    announcement = models.ForeignKey(Announcement, on_delete=models.CASCADE, related_name="+")
    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="+")
    attempted_at = models.DateTimeField(auto_now_add=True)
    ok = models.BooleanField()
    detail = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        indexes = [models.Index(fields=["announcement", "member"])]

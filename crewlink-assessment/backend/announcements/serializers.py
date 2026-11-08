from django.utils import timezone
from rest_framework import serializers

from core.models import Member
from core.permissions import require_account
from core.scoping import LocalTripwireMixin

from .models import Announcement, AnnouncementRecipient


class AnnouncementSerializer(LocalTripwireMixin, serializers.ModelSerializer):
    """Leader view of an announcement. Server-controlled fields are read-only,
    so a client can't set its own local, status, version or approver."""

    class Meta:
        model = Announcement
        fields = [
            "id", "local", "title", "body", "push_preview", "needs_ack",
            "kind", "event_at", "audience_classification", "supersedes",
            "status", "version", "ai_generated",
            "created_by", "approved_by", "approved_at", "sent_at", "created_at",
        ]
        read_only_fields = [
            "id", "local", "status", "version", "ai_generated",
            "created_by", "approved_by", "approved_at", "sent_at", "created_at",
        ]

    def validate(self, attrs):
        account = require_account(self.context["request"])

        def value(field):
            # On a partial edit, fall back to what's already saved.
            if field in attrs:
                return attrs[field]
            return getattr(self.instance, field, None) if self.instance else None

        kind = value("kind") or Announcement.Kind.GENERAL
        event_at = value("event_at")
        if kind == Announcement.Kind.MEETING:
            if event_at is None:
                raise serializers.ValidationError({"event_at": "A meeting needs an event time."})
            if event_at <= timezone.now():
                raise serializers.ValidationError({"event_at": "The meeting time is already in the past."})

        supersedes = value("supersedes")
        if supersedes is not None and supersedes.local_id != account.local_id:
            # Same answer as "doesn't exist": don't confirm another local's ids.
            raise serializers.ValidationError({"supersedes": "Announcement not found."})

        # An audience that matches nobody (say, a typo in the classification)
        # would "send" to 0 people and look successful. Refuse it up front.
        classification = value("audience_classification") or ""
        audience = Member.objects.filter(local_id=account.local_id, status=Member.Status.ACTIVE)
        if classification:
            audience = audience.filter(classification=classification)
        if not audience.exists():
            raise serializers.ValidationError(
                {"audience_classification": "No active members in your local match this audience."})
        return attrs


class InboxItemSerializer(LocalTripwireMixin, serializers.ModelSerializer):
    """Member view: the announcement plus the member's OWN status.
    Never includes other members, emails or audience size."""

    announcement_id = serializers.UUIDField(source="announcement.id", read_only=True)
    title = serializers.CharField(source="announcement.title", read_only=True)
    body = serializers.CharField(source="announcement.body", read_only=True)
    push_preview = serializers.CharField(source="announcement.push_preview", read_only=True)
    needs_ack = serializers.BooleanField(source="announcement.needs_ack", read_only=True)
    kind = serializers.CharField(source="announcement.kind", read_only=True)
    event_at = serializers.DateTimeField(source="announcement.event_at", read_only=True)
    has_happened = serializers.SerializerMethodField()

    class Meta:
        model = AnnouncementRecipient
        fields = [
            "announcement_id", "title", "body", "push_preview", "needs_ack",
            "kind", "event_at", "has_happened",
            "received_at", "read_at", "acked_at", "rsvp",
        ]
        read_only_fields = fields

    def get_has_happened(self, row):
        # Drives "this meeting has already happened" in the member app.
        event_at = row.announcement.event_at
        return event_at is not None and event_at <= timezone.now()

from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, Q, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
# DRF's version, not Django's: it also turns a malformed UUID into 404 (Django's raises
# ValidationError -> 500 on Postgres; SQLite never validates, so it hid this bug).
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

from core.scoping import LocalScopedMixin

from . import ai, sending, tasks
from .models import Announcement, AnnouncementRecipient
from .serializers import AnnouncementSerializer, DraftFromNoteSerializer, InboxItemSerializer

EDITABLE = (Announcement.Status.DRAFT, Announcement.Status.APPROVED)


class AIDraftThrottle(UserRateThrottle):
    """Per-leader limit on AI calls (they cost money). Counted in the default cache,
    which is Redis, so the limit holds across both API instances."""
    scope = "ai_draft"


class AnnouncementViewSet(LocalScopedMixin,
                          mixins.ListModelMixin,
                          mixins.RetrieveModelMixin,
                          mixins.CreateModelMixin,
                          viewsets.GenericViewSet):
    """Leaders only. Every query is already filtered to the leader's local."""

    allowed_roles = ("leader",)
    queryset = Announcement.objects.order_by("-created_at")
    serializer_class = AnnouncementSerializer
    http_method_names = ["get", "post", "patch", "head", "options"]

    def perform_create(self, serializer):
        # local and author come from the caller's account, never the request body.
        serializer.save(local_id=self.account.local_id, created_by=self.account)

    def partial_update(self, request, pk=None):
        with transaction.atomic():
            # Lock the row so an edit and an approve can't interleave.
            obj = get_object_or_404(self.get_queryset().select_for_update(), pk=pk)
            if obj.status not in EDITABLE:
                return Response({"detail": f"Can't edit an announcement that is {obj.status}."},
                                status=status.HTTP_409_CONFLICT)
            serializer = self.get_serializer(obj, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            # Any edit clears approval: a human must approve the text that actually goes out.
            serializer.save(status=Announcement.Status.DRAFT, approved_by=None,
                            approved_at=None, version=obj.version + 1)
        return Response(serializer.data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        obj = self.get_object()  # 404 if it's another local's
        try:
            version = int(request.data.get("version"))
        except (TypeError, ValueError):
            return Response({"version": "Send the version you reviewed."},
                            status=status.HTTP_400_BAD_REQUEST)

        # One atomic statement: approve only if it's still a draft AND still the
        # exact version this leader looked at.
        changed = Announcement.objects.filter(
            pk=obj.pk, local_id=self.account.local_id,
            status=Announcement.Status.DRAFT, version=version,
        ).update(status=Announcement.Status.APPROVED,
                 approved_by=self.account, approved_at=timezone.now())

        obj.refresh_from_db()
        if not changed:
            if obj.status == Announcement.Status.APPROVED and obj.version == version:
                pass  # already approved, same text: a repeat click is harmless
            elif obj.version != version:
                return Response({"detail": "It was edited since you reviewed it. Review the new version."},
                                status=status.HTTP_409_CONFLICT)
            else:
                return Response({"detail": f"Can't approve an announcement that is {obj.status}."},
                                status=status.HTTP_409_CONFLICT)
        return Response(self.get_serializer(obj).data)

    @action(detail=False, methods=["post"], url_path="draft-from-note",
            throttle_classes=[AIDraftThrottle])
    def draft_from_note(self, request):
        """Messy note -> AI title/body/preview, saved as a DRAFT only.

        Throttled per leader, in the shared Redis cache (holds across instances).
        If the provider fails, returns 503 and saves nothing: the leader writes it
        by hand with the normal create endpoint.
        """
        incoming = DraftFromNoteSerializer(data=request.data)
        incoming.is_valid(raise_exception=True)
        options = dict(incoming.validated_data)
        note = options.pop("note")

        # Check the leader's own choices BEFORE paying for an AI call: a typo in the
        # audience, or a meeting time in the past, should fail fast and free.
        self.get_serializer(data={"title": "-", "body": "-", "push_preview": "-", **options}) \
            .is_valid(raise_exception=True)

        drafter = ai.get_drafter()
        try:
            text = drafter.draft(note)  # only the note goes to the provider
        except ai.AIUnavailable:
            return Response(
                {"detail": "The AI helper is unavailable right now. Please write the announcement yourself.",
                 "code": "ai_unavailable"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE)

        # Same serializer and validation as a hand-written draft (audience must match
        # someone, meeting time in the future...). local and author come from the account.
        serializer = self.get_serializer(data={**text, **options})
        serializer.is_valid(raise_exception=True)
        serializer.save(local_id=self.account.local_id, created_by=self.account,
                        ai_generated=True)  # status stays `draft`: a human must approve
        return Response({**serializer.data, "ai_provider": drafter.name},
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def send(self, request, pk=None):
        """Leader waits ~100 ms. Everything slow happens in workers."""
        obj = self.get_object()  # 404 if it's another local's
        now = timezone.now()
        if (obj.status in EDITABLE and obj.event_at is not None and obj.event_at <= now):
            return Response({"detail": "The meeting time has already passed."},
                            status=status.HTTP_409_CONFLICT)

        with transaction.atomic():
            # Guard 1 (Rule 2): approved -> queued happens exactly once.
            first = sending.request_send(obj.pk, self.account.local_id, now)
            if first:
                # Only after commit: a fast worker must never see the old status.
                transaction.on_commit(lambda: tasks.enqueue(tasks.fan_out_task, obj.pk))

        obj.refresh_from_db()
        if not first and obj.status == Announcement.Status.DRAFT:
            return Response({"detail": "Approve it before sending."},
                            status=status.HTTP_409_CONFLICT)
        # First click, double click and network retry all get the same answer.
        return Response({"id": str(obj.pk), "status": obj.status, "sent_at": obj.sent_at},
                        status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=["get"])
    def stats(self, request, pk=None):
        """Counts for the live status screen, cached in Redis for STATS_TTL seconds.

        Any number of viewers -> at most one COUNT query per announcement per 2 s.
        The cache key includes the caller's local, and is only ever filled after the
        scoped lookup succeeded, so a cached answer can't cross locals.
        """
        key = f"stats:{self.account.local_id}:{pk}"
        data = cache.get(key)
        if data is None:
            obj = self.get_object()  # 404 if it's another local's
            data = recipient_counts(obj)
            cache.set(key, data, STATS_TTL)
        return Response(data)


STATS_TTL = 2  # seconds


def recipient_counts(ann):
    """One aggregate query over the announcement's recipient rows."""
    P = AnnouncementRecipient.PushStatus
    counts = ann.recipients.aggregate(
        total=Count("id"),
        in_progress=Count("id", filter=Q(push_status__in=[P.PENDING, P.SENDING])),
        sent=Count("id", filter=Q(push_status=P.SENT)),        # accepted by the push provider
        failed=Count("id", filter=Q(push_status=P.FAILED)),
        skipped=Count("id", filter=Q(push_status=P.SKIPPED)),
        on_device=Count("id", filter=Q(received_at__isnull=False)),
        read=Count("id", filter=Q(read_at__isnull=False)),
        acknowledged=Count("id", filter=Q(acked_at__isnull=False)),
        coming=Count("id", filter=Q(rsvp=AnnouncementRecipient.Rsvp.COMING)),
        cant_come=Count("id", filter=Q(rsvp=AnnouncementRecipient.Rsvp.CANT)),
    )
    counts["not_seen"] = counts["total"] - counts["read"]
    return {
        "announcement_id": str(ann.pk),
        "status": ann.status,
        "needs_ack": ann.needs_ack,
        "event_at": ann.event_at.isoformat() if ann.event_at else None,
        "counts": counts,
        "as_of": timezone.now().isoformat(),
    }


class InboxViewSet(LocalScopedMixin,
                   mixins.ListModelMixin,
                   mixins.RetrieveModelMixin,
                   viewsets.GenericViewSet):
    """Members only. A member sees only their OWN recipient rows, looked up by
    announcement id. Anything else, including a callout that went to a colleague
    but not to them, is a 404."""

    allowed_roles = ("member",)
    queryset = AnnouncementRecipient.objects.select_related("announcement")
    serializer_class = InboxItemSerializer
    lookup_field = "announcement_id"
    lookup_url_kwarg = "pk"

    def get_queryset(self):
        return (super().get_queryset()
                .filter(member_id=self.account.member_id)
                .order_by("-created_at"))

    def _mark(self, row, **fields):
        """Set each timestamp only if it's still empty: repeats keep the first time."""
        now = Value(timezone.now())
        AnnouncementRecipient.objects.filter(pk=row.pk).update(
            **{f: Coalesce(f, now) for f in fields})
        row.refresh_from_db()

    def list(self, request, *args, **kwargs):
        # The app syncing its inbox is the first proof a callout reached the phone.
        self.get_queryset().filter(received_at__isnull=True).update(received_at=timezone.now())
        return super().list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        row = self.get_object()
        self._mark(row, received_at=True)
        return Response(self.get_serializer(row).data)

    @action(detail=True, methods=["post"])
    def read(self, request, pk=None):
        row = self.get_object()
        self._mark(row, received_at=True, read_at=True)
        return Response(self.get_serializer(row).data)

    @action(detail=True, methods=["post"])
    def ack(self, request, pk=None):
        row = self.get_object()
        if not row.announcement.needs_ack:
            return Response({"detail": "This callout doesn't ask for acknowledgement."},
                            status=status.HTTP_400_BAD_REQUEST)
        # Acknowledging means you've read it, so read_at is filled in too.
        self._mark(row, received_at=True, read_at=True, acked_at=True)
        return Response(self.get_serializer(row).data)

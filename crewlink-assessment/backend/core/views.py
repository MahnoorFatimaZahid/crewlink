from django.contrib.auth import authenticate
from django.db.models import Count
from rest_framework import serializers, status
from rest_framework.authtoken.models import Token
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.generics import GenericAPIView
from rest_framework.views import APIView

from .models import Member
from .permissions import HasRole, require_account
from .scoping import LocalScopedMixin


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(write_only=True)


class LoginView(APIView):
    """The only endpoint open to anonymous users (it's on the guard test's allow-list).

    Throttled per IP via the shared Redis cache, so the limit holds across instances.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request):
        data = LoginSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        user = authenticate(username=data.validated_data["username"],
                            password=data.validated_data["password"])
        account = getattr(user, "account", None) if user else None
        # Same message for "wrong password", "no such user" and "no account":
        # the response must not reveal which usernames exist.
        if account is None or not user.is_active:
            return Response({"detail": "Invalid username or password."},
                            status=status.HTTP_400_BAD_REQUEST)
        token, _ = Token.objects.get_or_create(user=user)
        return Response({
            "token": token.key,
            "role": account.role,
            "local_id": str(account.local_id),
            "local_name": account.local.name,
        })


class MeView(APIView):
    """Who am I? Returns only the caller's own account, so it needs no local scoping."""

    permission_classes = [HasRole]
    allowed_roles = ("leader", "member")

    def get(self, request):
        account = require_account(request)
        return Response({
            "username": request.user.username,
            "role": account.role,
            "local_id": str(account.local_id),
            "local_name": account.local.name,
            "member_id": str(account.member_id) if account.member_id else None,
        })


class ClassificationListView(LocalScopedMixin, GenericAPIView):
    """Leaders: who a callout would reach, for the audience picker.

    Returns the leader's own local and its active-member counts per classification.
    Scoped like every other view, so another local's roster (even its job titles)
    never shows up.
    """

    allowed_roles = ("leader",)
    queryset = Member.objects.all()

    def get(self, request):
        active = self.get_queryset().filter(status=Member.Status.ACTIVE)
        groups = (active.values("classification")
                  .annotate(active_members=Count("id"))
                  .order_by("classification"))
        account = self.account
        return Response({
            "local": {"id": str(account.local_id), "name": account.local.name},
            "active_members": active.count(),
            "classifications": [{"name": g["classification"], "active_members": g["active_members"]}
                                for g in groups],
        })

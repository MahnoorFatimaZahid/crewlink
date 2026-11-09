"""
Rule 1, the "between locals" boundary, enforced in one place.

LocalScopedMixin: every query a view makes is filtered to the caller's local.
  Another local's record is simply not found -> 404 (we don't confirm it exists).

LocalTripwireMixin: the last check before data leaves the server. If any object
  in a response belongs to another local (say, a buggy raw query slipped past the
  scoping), the response is blocked and logged instead of leaking.

The guard test (core/test_guard.py) fails the build if a view skips these.
"""
import logging

from rest_framework.exceptions import APIException

from .permissions import HasRole, require_account

security_log = logging.getLogger("crewlink.security")


class LocalScopedMixin:
    permission_classes = [HasRole]
    allowed_roles = ()  # each view must set this; empty means nobody gets in

    @property
    def account(self):
        return require_account(self.request)

    def get_queryset(self):
        return super().get_queryset().filter(local_id=self.account.local_id)


class LocalBoundaryViolation(APIException):
    status_code = 500
    default_detail = "Internal error."  # say nothing about what was blocked
    default_code = "internal_error"


class LocalTripwireMixin:
    """Add to every serializer that returns local-owned data."""

    def to_representation(self, instance):
        request = self.context.get("request")
        account = require_account(request) if request else None
        if account is None or instance.local_id != account.local_id:
            security_log.critical(
                "RULE1_TRIPWIRE blocked %s id=%s local=%s for account=%s local=%s path=%s",
                type(instance).__name__, instance.pk, instance.local_id,
                getattr(account, "pk", None), getattr(account, "local_id", None),
                getattr(request, "path", None),
            )
            raise LocalBoundaryViolation()
        return super().to_representation(instance)

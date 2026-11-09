"""
Rule 1, the "member vs leader" boundary.

Every view either:
  - keeps the default DenyAll (nobody gets in), or
  - uses HasRole and declares allowed_roles = ("leader",) / ("member",).

The caller's role and local always come from their Account row in the
database, never from anything the client sends.
"""
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission


class DenyAll(BasePermission):
    """Default for every view. A view that forgets to declare permissions is
    unreachable (fails closed) instead of open to everyone (fails open)."""

    def has_permission(self, request, view):
        return False


def get_account(request):
    """The logged-in user's Account, or None (anonymous, or a user with no Account)."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None
    # Missing reverse one-to-one raises RelatedObjectDoesNotExist, a subclass of
    # AttributeError, so getattr's default catches it.
    return getattr(user, "account", None)


def require_account(request):
    account = get_account(request)
    if account is None:
        raise PermissionDenied("No account for this login.")
    return account


class HasRole(BasePermission):
    """Lets a request through only if the caller's role is in view.allowed_roles.

    A view that uses HasRole but forgets allowed_roles still denies everyone,
    so forgetting fails closed here too.
    """

    def has_permission(self, request, view):
        allowed = getattr(view, "allowed_roles", None)
        if not allowed:
            return False
        account = get_account(request)
        return account is not None and account.role in allowed

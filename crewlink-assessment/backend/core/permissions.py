from rest_framework.permissions import BasePermission


class DenyAll(BasePermission):
    """Default for every view. A view that forgets to declare permissions is
    unreachable (fails closed) instead of open to everyone (fails open)."""

    def has_permission(self, request, view):
        return False

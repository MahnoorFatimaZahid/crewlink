from django.http import JsonResponse
from django.urls import include, path
from rest_framework.routers import SimpleRouter

from announcements.views import AnnouncementViewSet, InboxViewSet
from core.views import ClassificationListView, LoginView, MeView


def health(request):
    # Plain Django view (not DRF), so DenyAll doesn't apply. Used by nginx/docker checks.
    # On the guard test's PUBLIC_FUNCTION_VIEWS allow-list.
    return JsonResponse({"ok": True})


# SimpleRouter (not DefaultRouter): no browsable API root view to secure.
router = SimpleRouter(trailing_slash=False)
router.register("announcements", AnnouncementViewSet, basename="announcement")
router.register("me/announcements", InboxViewSet, basename="inbox")

urlpatterns = [
    path("health/", health),
    path("api/auth/login", LoginView.as_view(), name="login"),
    path("api/me", MeView.as_view(), name="me"),
    path("api/classifications", ClassificationListView.as_view(), name="classifications"),
    path("api/", include(router.urls)),
]

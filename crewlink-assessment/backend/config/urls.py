from django.http import JsonResponse
from django.urls import path


def health(request):
    # Plain Django view (not DRF), so DenyAll doesn't apply. Used by nginx/docker checks.
    return JsonResponse({"ok": True})


urlpatterns = [
    path("health/", health),
    # API routes are added in the next stage.
]

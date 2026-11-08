"""
Rule 1, "the endpoint someone adds next year".

Walks every URL in the project and fails the build if a view:
  - is a plain function view (can't be checked),
  - doesn't use the HasRole permission or doesn't declare allowed_roles,
  - doesn't inherit LocalScopedMixin (so its queries aren't filtered by local),
unless it's on a short, reviewed allow-list below.
"""
from django.test import SimpleTestCase
from django.urls import URLPattern, URLResolver, get_resolver
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from core.permissions import HasRole
from core.scoping import LocalScopedMixin

# Each entry needs a reason. Adding to these lists should be a reviewed decision.
PUBLIC = {
    "LoginView": "anonymous by design; throttled",
}
PUBLIC_FUNCTION_VIEWS = {
    "health/": "plain Django liveness check for docker/nginx; returns only {ok: true}",
}
NOT_LOCAL_SCOPED = {
    "MeView": "returns only the caller's own account; queries nothing else",
}


def all_views(patterns=None, prefix=""):
    patterns = get_resolver().url_patterns if patterns is None else patterns
    for p in patterns:
        if isinstance(p, URLResolver):
            yield from all_views(p.url_patterns, prefix + str(p.pattern))
        elif isinstance(p, URLPattern):
            view = getattr(p.callback, "cls", None) or getattr(p.callback, "view_class", None)
            yield prefix + str(p.pattern), view


def find_problems(routes):
    problems = []
    for route, view in routes:
        if view is None:
            if route in PUBLIC_FUNCTION_VIEWS:
                continue
            problems.append(f"{route}: function view, can't be checked")
            continue
        name = view.__name__
        if name in PUBLIC:
            continue
        if HasRole not in getattr(view, "permission_classes", []):
            problems.append(f"{route} ({name}): doesn't use HasRole")
        if not getattr(view, "allowed_roles", None):
            problems.append(f"{route} ({name}): no allowed_roles")
        if name not in NOT_LOCAL_SCOPED and not issubclass(view, LocalScopedMixin):
            problems.append(f"{route} ({name}): not LocalScopedMixin")
    return problems


class EveryEndpointIsGuardedTest(SimpleTestCase):
    def test_every_real_endpoint_is_guarded(self):
        problems = find_problems(all_views())
        self.assertEqual(problems, [], "\n".join(problems))

    def test_guard_flags_a_view_that_skips_the_base_class(self):
        # The exact mistake from our design discussion: roles set, local filter forgotten.
        class MemberExportView(APIView):
            permission_classes = [HasRole]
            allowed_roles = ("leader",)

        problems = find_problems([("api/members/export", MemberExportView)])
        self.assertEqual(problems, ["api/members/export (MemberExportView): not LocalScopedMixin"])

    def test_guard_flags_a_scoped_view_that_opens_its_permissions(self):
        class OopsView(LocalScopedMixin, APIView):
            permission_classes = [AllowAny]
            allowed_roles = ("leader",)

        problems = find_problems([("api/oops", OopsView)])
        self.assertIn("api/oops (OopsView): doesn't use HasRole", problems)

    def test_guard_flags_function_views(self):
        problems = find_problems([("api/legacy", None)])
        self.assertEqual(len(problems), 1)

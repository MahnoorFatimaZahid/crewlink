"""
The login rate limit must not be dodgeable with a fake X-Forwarded-For header.

DRF identifies the client from X-Forwarded-For. With NUM_PROXIES unset it uses the
WHOLE header, which the client controls, so a new fake value per attempt would mean
a new "client" per attempt and unlimited password guesses.
"""
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient


LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
BASE = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.TokenAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["core.permissions.DenyAll"],
    "DEFAULT_THROTTLE_RATES": {"login": "10/min", "ai_draft": "20/min"},
}


@override_settings(CACHES=LOCMEM)
class LoginThrottleSpoofingTests(TestCase):
    def setUp(self):
        cache.clear()

    def attempts(self, n, xff_for=lambda i: f"10.0.0.{i}", remote="172.18.0.5"):
        c = APIClient()
        return [c.post("/api/auth/login", {"username": "x", "password": "y"}, format="json",
                       HTTP_X_FORWARDED_FOR=xff_for(i), REMOTE_ADDR=remote).status_code
                for i in range(n)]

    @override_settings(REST_FRAMEWORK={**BASE, "NUM_PROXIES": 0})
    def test_local_mode_ignores_client_sent_forwarded_for(self):
        self.assertEqual(self.attempts(11)[-1], 429)

    @override_settings(REST_FRAMEWORK={**BASE, "NUM_PROXIES": 1})
    def test_behind_nginx_a_fake_prefix_does_not_help(self):
        # nginx overwrites the header with the real address, but even if a client's
        # value got through as a prefix, DRF reads only the LAST entry (nginx's).
        codes = self.attempts(11, xff_for=lambda i: f"6.6.6.{i}, 203.0.113.7")
        self.assertEqual(codes[-1], 429)

    @override_settings(REST_FRAMEWORK={**BASE, "NUM_PROXIES": 1})
    def test_behind_nginx_different_real_clients_get_separate_limits(self):
        self.attempts(10, xff_for=lambda i: "203.0.113.7")          # one leader uses up their 10
        other = self.attempts(1, xff_for=lambda i: "198.51.100.9")  # someone else
        self.assertEqual(other, [400])                              # not blocked

    @override_settings(REST_FRAMEWORK={**BASE, "NUM_PROXIES": None})
    def test_the_bug_this_guards_against(self):
        """Without NUM_PROXIES, a new fake header per attempt dodges the limit."""
        self.assertNotIn(429, self.attempts(11))


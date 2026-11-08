"""GET /api/classifications: the audience dropdown, scoped to the leader's own local."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core.models import Account, Local, Member

User = get_user_model()


def member(local, name, classification, status="active"):
    return Member.objects.create(local=local, full_name=name, email=f"{name}@example.com",
                                 classification=classification, status=status)


def login(username, local, role, member=None):
    user = User.objects.create_user(username=username, password="x")
    Account.objects.create(user=user, local=local, role=role, member=member)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=user).key}")
    return client


class ClassificationListTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.l27 = Local.objects.create(name="Local 27")
        cls.l12 = Local.objects.create(name="Local 12")
        cls.m27 = member(cls.l27, "a", "Foreman")
        member(cls.l27, "b", "Foreman")                       # duplicate -> listed once
        member(cls.l27, "c", "Apprentice 1st Year")
        member(cls.l27, "d", "Retiree Group", status="retired")  # no active member -> hidden
        member(cls.l12, "e", "Local 12 Only Title")           # other local -> hidden

    def test_leader_gets_own_locals_active_classifications_with_counts(self):
        r = login("lead27", self.l27, "leader").get("/api/classifications")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["local"]["name"], "Local 27")
        self.assertEqual(r.data["active_members"], 3)  # retired member not counted
        self.assertEqual(r.data["classifications"], [
            {"name": "Apprentice 1st Year", "active_members": 1},
            {"name": "Foreman", "active_members": 2},
        ])

    def test_member_gets_403_and_anonymous_gets_401(self):
        self.assertEqual(login("mem27", self.l27, "member", self.m27)
                         .get("/api/classifications").status_code, 403)
        self.assertEqual(APIClient().get("/api/classifications").status_code, 401)

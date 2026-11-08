import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class Local(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True)  # "Local 27"

    def __str__(self):
        return self.name


class Member(models.Model):
    """The union roster. Most members never log in; that's what Account is for."""

    class Status(models.TextChoices):
        ACTIVE = "active"
        RETIRED = "retired"
        SUSPENDED = "suspended"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    local = models.ForeignKey(Local, on_delete=models.PROTECT, related_name="members")
    full_name = models.CharField(max_length=200)
    email = models.EmailField()
    classification = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)

    class Meta:
        indexes = [
            # Building an audience: "active members of this local (and classification)".
            models.Index(fields=["local", "status", "classification"]),
        ]

    def __str__(self):
        return self.full_name


class Account(models.Model):
    """Who can log in, and with what powers. Rule 1 reads its local and role."""

    class Role(models.TextChoices):
        LEADER = "leader"
        MEMBER = "member"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="account")
    local = models.ForeignKey(Local, on_delete=models.PROTECT, related_name="accounts")
    role = models.CharField(max_length=10, choices=Role.choices)
    member = models.OneToOneField(Member, null=True, blank=True,
                                  on_delete=models.PROTECT, related_name="account")

    class Meta:
        constraints = [
            # A member login must point at a roster row. Enforced by Postgres.
            models.CheckConstraint(
                condition=~models.Q(role="member") | models.Q(member__isnull=False),
                name="member_account_needs_member",
            ),
        ]

    def clean(self):
        # A login must never point at another local's member.
        # (A CHECK can't compare across tables, so this lives in Python.)
        if self.member_id and self.member.local_id != self.local_id:
            raise ValidationError("Account local must match its member's local.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def is_leader(self):
        return self.role == self.Role.LEADER

    def __str__(self):
        return f"{self.user} ({self.role}, {self.local})"

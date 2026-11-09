"""
python manage.py seed

Re-runnable: every run deletes the seed locals' data and recreates it with the
SAME ids (uuid5 from fixed names), so README's Test Accounts stay valid forever.
"""
import json
import random
import uuid
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from announcements.models import Announcement, AnnouncementRecipient
from core.models import Account, Local, Member

NS = uuid.UUID("6f1c3e1a-2b9d-4c55-9a0e-27c0ffee2700")  # fixed namespace for stable ids
PASSWORD = "crewlink-demo-2026"

LOCALS = [("Local 27", 2000), ("Local 12", 200)]
CLASSIFICATIONS = ["Journeyman Wireman", "Apprentice 1st Year",
                   "Apprentice 3rd Year", "Foreman"]
FIRST = ["Ali", "Sara", "James", "Maria", "Omar", "Grace", "Luis", "Aisha",
         "Tom", "Nadia", "Kevin", "Fatima", "Daniel", "Rosa", "Hassan", "Emily"]
LAST = ["Khan", "Smith", "Garcia", "Okafor", "Brown", "Ahmed", "Lopez",
        "Nguyen", "Walker", "Malik", "Turner", "Diaz", "Clarke", "Shah"]


def sid(*parts):
    return uuid.uuid5(NS, ":".join(str(p) for p in parts))


class Command(BaseCommand):
    help = "Seed 2 locals, members, 4 logins and one already-sent announcement (re-runnable)."

    @transaction.atomic
    def handle(self, *args, **opts):
        rng = random.Random(27)  # same "random" data every run
        now = timezone.now()
        local_ids = [sid("local", name) for name, _ in LOCALS]

        # 1. Wipe previous seed data (children first because of PROTECT foreign keys).
        Announcement.objects.filter(local_id__in=local_ids).delete()
        User.objects.filter(account__local_id__in=local_ids).delete()  # cascades accounts
        Member.objects.filter(local_id__in=local_ids).delete()
        Local.objects.filter(id__in=local_ids).delete()

        out = {}
        for (name, size), local_id in zip(LOCALS, local_ids):
            num = name.split()[-1]
            local = Local.objects.create(id=local_id, name=name)

            # 2. Members: mostly active, some retired/suspended (they must NOT get callouts).
            members = []
            for i in range(size):
                f, l = rng.choice(FIRST), rng.choice(LAST)
                status = rng.choices(["active", "retired", "suspended"], [90, 7, 3])[0]
                members.append(Member(
                    id=sid("member", num, i), local=local, full_name=f"{f} {l}",
                    email=f"{f.lower()}.{l.lower()}.{i}@local{num}.example",
                    classification=rng.choice(CLASSIFICATIONS), status=status,
                ))
            Member.objects.bulk_create(members)

            # 3. One leader and one member login per local.
            leader_user = User.objects.create_user(f"leader{num}", password=PASSWORD)
            leader = Account.objects.create(user=leader_user, local=local, role="leader")

            login_member = next(m for m in members if m.status == "active")
            member_user = User.objects.create_user(f"member{num}", password=PASSWORD)
            Account.objects.create(user=member_user, local=local, role="member",
                                   member=login_member)

            out[name] = {
                "local_id": str(local.id),
                "leader": {"username": leader_user.username, "password": PASSWORD},
                "member": {"username": member_user.username, "password": PASSWORD,
                           "member_id": str(login_member.id)},
            }

            # 4. Larger local only: one already-sent announcement with recipient rows.
            if size == max(s for _, s in LOCALS):
                out[name]["sent_announcement_id"] = str(
                    self._seed_sent_announcement(local, leader, members, now, rng))

        self.stdout.write(json.dumps(out, indent=2))

    def _seed_sent_announcement(self, local, leader, members, now, rng):
        sent_at = now - timedelta(days=3)
        ann = Announcement.objects.create(
            id=sid("announcement", local.name, "seed-1"), local=local,
            title="Emergency meeting: Westside contractor",
            body="Emergency meeting Thursday 6pm at the hall about the contractor "
                 "pulling crews off the Westside job. Everyone needs to attend.",
            push_preview="Emergency meeting Thu 6pm at the hall: Westside contractor. Please attend.",
            needs_ack=True, kind="meeting", event_at=sent_at + timedelta(days=1),
            status="sent", created_by=leader, approved_by=leader,
            approved_at=sent_at, sent_at=sent_at,
        )
        rows = []
        for m in members:
            if m.status != "active":
                continue  # audience = active members only
            r = rng.random()  # realistic spread: not everyone sees it
            rows.append(AnnouncementRecipient(
                announcement=ann, member=m, local=local,
                push_status="sent", push_attempts=1, sent_at=sent_at,
                received_at=sent_at + timedelta(minutes=5) if r < 0.85 else None,
                read_at=sent_at + timedelta(minutes=30) if r < 0.65 else None,
                acked_at=sent_at + timedelta(minutes=35) if r < 0.45 else None,
            ))
        AnnouncementRecipient.objects.bulk_create(rows)
        return ann.id

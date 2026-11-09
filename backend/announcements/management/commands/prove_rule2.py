"""
The DevOps bonus proof: Rule 2 holds across two API instances.

    docker compose exec api1 python manage.py prove_rule2 \
        --hosts http://api1:8000,http://api2:8000 --username leader27 --password ...

Every request goes to api1 or api2 at random, over real HTTP (the same API a client
uses). It fires many Send requests at the same moment, waits for the send to finish,
then checks the database: one recipient row and exactly one successful push per active
member, and no integrity problems. Exit code 1 if anything is wrong.

Runs inside the Docker network so it can reach each instance directly; the instances
aren't published on the host (that would let clients skip nginx).
"""
import json
import random
import threading
import time
import urllib.error
import urllib.request
from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from announcements.integrity import find_problems
from announcements.models import Announcement, AnnouncementRecipient, PushLog
from core.models import Member


def call(host, method, path, token=None, body=None):
    req = urllib.request.Request(host + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Token {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class Command(BaseCommand):
    help = "Prove Rule 2 (no double delivery) across multiple API instances."

    def add_arguments(self, parser):
        parser.add_argument("--hosts", default="http://api1:8000,http://api2:8000")
        parser.add_argument("--username", required=True)
        parser.add_argument("--password", required=True)
        parser.add_argument("--sends", type=int, default=20, help="Simultaneous Send requests.")
        parser.add_argument("--timeout", type=int, default=180, help="Seconds to wait for the send.")

    def handle(self, *args, hosts, username, password, sends, timeout, **opts):
        hosts = [h.rstrip("/") for h in hosts.split(",")]
        pick = lambda: random.choice(hosts)  # noqa: E731
        say = self.stdout.write

        status, data = call(pick(), "POST", "/api/auth/login",
                            body={"username": username, "password": password})
        if status != 200:
            raise CommandError(f"login failed: {status} {data}")
        token = data["token"]

        status, ann = call(pick(), "POST", "/api/announcements", token, {
            "title": f"Rule 2 proof {time.strftime('%H:%M:%S')}",
            "body": "Sent to two instances at once to prove nobody gets it twice.",
            "push_preview": "Rule 2 proof"})
        if status != 201:
            raise CommandError(f"create failed: {status} {ann}")
        status, _ = call(pick(), "POST", f"/api/announcements/{ann['id']}/approve", token,
                         {"version": ann["version"]})
        if status != 200:
            raise CommandError(f"approve failed: {status}")
        say(f"announcement {ann['id']} created and approved")

        # Fire all the Sends at the same instant, each at a random instance.
        barrier = threading.Barrier(sends)
        results = []

        def fire():
            host = pick()
            barrier.wait()
            results.append((host, call(host, "POST", f"/api/announcements/{ann['id']}/send", token)[0]))

        threads = [threading.Thread(target=fire) for _ in range(sends)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        by_host = Counter(h for h, _ in results)
        codes = Counter(c for _, c in results)
        say(f"{sends} simultaneous sends -> per instance {dict(by_host)}, status codes {dict(codes)}")

        # Watch the counts (also through random instances) until the send finishes.
        deadline = time.time() + timeout
        while True:
            status, stats = call(pick(), "GET", f"/api/announcements/{ann['id']}/stats", token)
            c = stats.get("counts", {})
            say(f"  {stats.get('status')}: sent {c.get('sent')}/{c.get('total')}, "
                f"in progress {c.get('in_progress')}")
            if stats.get("status") == "sent":
                break
            if time.time() > deadline:
                raise CommandError("timed out waiting for the send (are the workers running?)")
            time.sleep(2)

        # The proof, straight from the database.
        a = Announcement.objects.get(pk=ann["id"])
        audience = Member.objects.filter(local_id=a.local_id, status=Member.Status.ACTIVE).count()
        rows = AnnouncementRecipient.objects.filter(announcement=a).count()
        pushes = Counter(PushLog.objects.filter(announcement=a, ok=True)
                         .values_list("member_id", flat=True))
        problems = find_problems()
        checks = {
            "every active member has exactly one recipient row": rows == audience,
            "every active member got a push": len(pushes) == audience,
            "nobody got more than one push": set(pushes.values()) <= {1},
            "both instances served requests": len(by_host) == len(hosts),
            "every Send request answered 202": set(codes) == {202},
            "check_integrity is clean": not problems,
        }
        say(f"audience {audience}, recipient rows {rows}, members pushed {len(pushes)}, "
            f"max pushes per member {max(pushes.values(), default=0)}")
        for name, ok in checks.items():
            say(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        for p in problems:
            self.stderr.write(f"  {p}")
        if not all(checks.values()):
            raise CommandError("Rule 2 proof FAILED")
        say(self.style.SUCCESS("Rule 2 holds across instances."))

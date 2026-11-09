# CrewLink: verification and local setup

Detail moved out of the [README](README.md): running without Docker, a full curl walkthrough, how Rules 1 and 2 were verified, and bugs found along the way. Accounts and ids are in the README under [TEST ACCOUNTS](README.md#test-accounts).

## Run it locally (no Docker)

Needs Python 3.12+, Node 20+, and Postgres with a `crewlink` user/database (password `crewlink`). Redis is optional.

```bash
cd backend
python -m venv .venv && .venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\...
export REDIS_URL=""            # no Redis: in-memory cache, and process_sends instead of Celery
# export POSTGRES_PORT=5433    # if your Postgres isn't on 5432
.venv/bin/python manage.py migrate && .venv/bin/python manage.py seed
.venv/bin/python manage.py runserver 8000          # terminal 1: API
.venv/bin/python manage.py process_sends           # terminal 2: sends (polls Postgres)

cd frontend && npm install && npm run dev          # terminal 3: http://localhost:3000
```

`process_sends` works without any queue because all send state lives in Postgres. Redis and Celery make it faster, not correct.

## Try the API with curl

All endpoints use `Authorization: Token <token>`. Routes have no trailing slash.

```bash
API=http://localhost:8000/api
login() { curl -s -X POST $API/auth/login -H 'Content-Type: application/json' \
  -d "{\"username\":\"$1\",\"password\":\"crewlink-demo-2026\"}" | sed -E 's/.*"token":"([^"]+)".*/\1/'; }
L27=$(login leader27); M27=$(login member27); L12=$(login leader12)
ANN=025b6566-bd49-598b-8c62-daf0017a9ac8     # Local 27's seeded, already-sent callout
```

**A member reads and acknowledges a callout:**

```bash
curl -s $API/me/announcements -H "Authorization: Token $M27"                          # inbox
curl -s -X POST $API/me/announcements/$ANN/read -H "Authorization: Token $M27"        # mark read
curl -s -X POST $API/me/announcements/$ANN/ack  -H "Authorization: Token $M27"        # acknowledge
```

Repeating `ack` keeps the first timestamp.

**Rule 1: Local 27 can't read Local 12's data (and vice versa):**

```bash
# Local 12's leader writes a draft
ID12=$(curl -s -X POST $API/announcements -H "Authorization: Token $L12" -H 'Content-Type: application/json' \
  -d '{"title":"Local 12 only","body":"Internal","push_preview":"Local 12 only"}' | sed -E 's/.*"id":"([^"]+)".*/\1/')

curl -s -o /dev/null -w '%{http_code}\n' $API/announcements/$ID12 -H "Authorization: Token $L27"   # 404
curl -s $API/announcements -H "Authorization: Token $L27" | grep -c "$ID12"                        # 0: not listed
curl -s -o /dev/null -w '%{http_code}\n' $API/announcements/$ANN -H "Authorization: Token $L12"    # 404
curl -s -o /dev/null -w '%{http_code}\n' $API/announcements -H "Authorization: Token $M27"         # 403: members can't use leader endpoints
```

Another local's record is a 404, exactly like one that doesn't exist, so ids can't be probed.

**The send flow:** create → approve (naming the version you reviewed) → send → watch counts:

```bash
NEW=$(curl -s -X POST $API/announcements/draft-from-note -H "Authorization: Token $L27" -H 'Content-Type: application/json' \
  -d '{"note":"EMERGENCY mtg thurs 6pm at the hall, everyone pls come"}' | sed -E 's/.*"id":"([^"]+)".*/\1/')
curl -s -X POST $API/announcements/$NEW/approve -H "Authorization: Token $L27" -H 'Content-Type: application/json' -d '{"version":1}'
curl -s -X POST $API/announcements/$NEW/send    -H "Authorization: Token $L27"      # 202; repeat it: still sent once
curl -s $API/announcements/$NEW/stats           -H "Authorization: Token $L27"
```

## How the two rules were verified (in detail)

**Automated: 111 tests, on real Postgres** (`docker compose exec api1 python manage.py test`). Many deliberately go around Django's validation to prove Postgres itself refuses bad data.

- **Rule 1:** every endpoint is tested for 401 / 403 / 404 across roles and locals, and a guard test walks every URL and fails the build if a view skips the role check or the local scoping. The guard test is itself tested against a deliberately broken view. Composite foreign keys make cross-local rows impossible at the database level, and a login-throttle test proves a forged `X-Forwarded-For` can't dodge the rate limit.
- **Rule 2:** double Send, re-run fan-out, parallel dispatch, a crash between claim and push, retries, and expired meetings. The concurrency tests use real threads and row locks on Postgres.

**Across two instances, in Docker:**

```bash
docker compose exec api1 python manage.py prove_rule2 --username leader27 --password crewlink-demo-2026
```

This fires 20 simultaneous Sends at api1 and api2 at random, waits for delivery, then checks the database. Result: 1,798 active members, 1,798 recipient rows, 1,798 members pushed, **at most 1 push per member**, all 6 checks passing.

```bash
curl -sI localhost:8000/health/ | grep -i x-upstream        # alternates between the two instances
docker compose exec api1 python manage.py check_integrity   # Rule 1 + Rule 2 invariants; exit 1 on failure
```

**Live, locally:** a send to 1,798 members with 7% of pushes failing on purpose (`PUSH_FAIL_RATE=0.07`) retried down to zero failures, ending with 1,798 successful pushes to 1,798 different members.

**Found by actually running it:**
- A malformed UUID crashed with a 500 on Postgres; SQLite would have hidden it. It's now a 404.
- A fan-out re-run could add members who joined after Send; it now runs only while `queued`.
- The push log was written after each batch, so a mid-batch crash left no evidence. It's now an intent log written before each push.
- nginx sent every new connection to api1, because each worker process kept its own round-robin counter. A shared `zone` fixed it.

## Project layout

```
backend/        Django + DRF
  core/           locals, members, accounts, login, Rule 1 scoping + guard test, seed
  announcements/  callouts, recipients, send path (sending.py, tasks.py), AI (ai.py), integrity checks
frontend/       Next.js leadership screen (proxies /api to Django)
nginx/          front door: round-robin across api1 + api2
DESIGN.md       the design document
```

# CrewLink: member callouts

Union leadership writes a callout, approves it, and sends it to every active member of their local (or one classification). Members get it in their inbox (push is only a nudge), and leadership watches sent / on device / read / acknowledged counts live.

- **Design and reasoning:** [DESIGN.md](DESIGN.md) (requirements, data model, send path, the two rules, diagram, what I cut)
- **Stack:** Django + DRF, Postgres, Redis + Celery, Next.js (TypeScript), nginx in front of two API instances, Gemini for AI drafts

## Run it (Docker)

```bash
docker compose up --build
```

The first build takes a few minutes. Then:

| What | Where |
|---|---|
| Leadership screen | http://localhost:8000 |
| API | http://localhost:8000/api/ |
| Health check | http://localhost:8000/health/ |

nginx is the only published port. It round-robins `/api/` across two identical API instances (`api1`, `api2`) and sends everything else to the Next.js screen. A one-shot `setup` service runs migrations and the seed before the APIs start, so the two instances never race on migrations.

Log in with `leader27` / `crewlink-demo-2026` (all accounts are under [Test accounts](#test-accounts)).

To reset everything: `docker compose down -v`.

**AI drafts:** with no key, a built-in fake provider tidies the note, so the whole flow works offline. To use real Gemini, create a git-ignored `.env` next to `docker-compose.yml` containing `GEMINI_API_KEY=...`. Never put a key in `.env.docker` (it's committed).

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

## How the two rules were verified

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

## Test accounts

Created by `python manage.py seed` (run automatically by Docker). The ids are fixed, so they're the same on every machine and every re-seed.

```json
{
  "base_url": "http://localhost:8000",
  "auth": "Token auth. POST /api/auth/login {username, password} -> {token}. Then send header 'Authorization: Token <token>'.",
  "password_for_all": "crewlink-demo-2026",
  "accounts": [
    {"username": "leader27", "role": "leader", "local": "Local 27"},
    {"username": "member27", "role": "member", "local": "Local 27", "member_id": "ccfee01f-f3c8-5ae7-bd4a-d9fd81da4dd2"},
    {"username": "leader12", "role": "leader", "local": "Local 12"},
    {"username": "member12", "role": "member", "local": "Local 12", "member_id": "81ed1f7d-90f5-5ffc-92dc-b86cc04dda61"}
  ],
  "locals": {
    "Local 27": {"id": "8f146c0d-bea8-56a0-ae3e-9eab0983ec6a", "members": 2000},
    "Local 12": {"id": "2a04f6de-fcc6-5f6b-8ff4-e047cd23d781", "members": 200}
  },
  "local27_announcement_id": "025b6566-bd49-598b-8c62-daf0017a9ac8",
  "local12_member_id": "81ed1f7d-90f5-5ffc-92dc-b86cc04dda61",
  "endpoints": [
    {"method": "POST", "path": "/api/auth/login", "auth": "none (throttled 10/min per IP)"},
    {"method": "GET", "path": "/api/me", "auth": "token, leader or member"},
    {"method": "GET", "path": "/api/classifications", "auth": "token, leader"},
    {"method": "GET", "path": "/api/announcements", "auth": "token, leader"},
    {"method": "POST", "path": "/api/announcements", "auth": "token, leader"},
    {"method": "GET", "path": "/api/announcements/{id}", "auth": "token, leader"},
    {"method": "PATCH", "path": "/api/announcements/{id}", "auth": "token, leader"},
    {"method": "POST", "path": "/api/announcements/draft-from-note", "auth": "token, leader (throttled 20/min)"},
    {"method": "POST", "path": "/api/announcements/{id}/approve", "auth": "token, leader; body {version}"},
    {"method": "POST", "path": "/api/announcements/{id}/send", "auth": "token, leader"},
    {"method": "GET", "path": "/api/announcements/{id}/stats", "auth": "token, leader"},
    {"method": "GET", "path": "/api/me/announcements", "auth": "token, member"},
    {"method": "GET", "path": "/api/me/announcements/{announcement_id}", "auth": "token, member"},
    {"method": "POST", "path": "/api/me/announcements/{announcement_id}/read", "auth": "token, member"},
    {"method": "POST", "path": "/api/me/announcements/{announcement_id}/ack", "auth": "token, member"}
  ]
}
```

The seeded Local 27 callout is a meeting that has already happened, sent 3 days ago, with a realistic spread: about 85% on device, 65% read and 45% acknowledged. Both locals include retired and suspended members, who never receive callouts.

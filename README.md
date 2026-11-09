# CrewLink: member callouts

Leadership writes, approves and sends a callout to every active member of their local; members read and acknowledge it in their inbox, and leadership watches live counts. Design: [DESIGN.md](DESIGN.md). Full walkthrough, local setup and test details: [VERIFICATION.md](VERIFICATION.md).

**Run:** `docker compose up --build`, then open http://localhost:8000 (screen), `/api/` (API), `/health/`. nginx round-robins across two API instances. Reset with `docker compose down -v`. AI drafts work offline; for real Gemini, put `GEMINI_API_KEY=...` in a git-ignored `.env`.

**Log in:** `leader27` (Local 27 leader) or `member27`, password `crewlink-demo-2026`. All accounts and ids are under [TEST ACCOUNTS](#test-accounts).

```bash
API=http://localhost:8000/api; ANN=025b6566-bd49-598b-8c62-daf0017a9ac8
login() { curl -s -X POST $API/auth/login -H 'Content-Type: application/json' -d "{\"username\":\"$1\",\"password\":\"crewlink-demo-2026\"}" | sed -E 's/.*"token":"([^"]+)".*/\1/'; }
L12=$(login leader12); M27=$(login member27)
curl -s $API/me/announcements -H "Authorization: Token $M27"                                    # member inbox
curl -s -X POST $API/me/announcements/$ANN/ack -H "Authorization: Token $M27"                  # acknowledge
curl -s -o /dev/null -w '%{http_code}\n' $API/announcements/$ANN -H "Authorization: Token $L12" # 404: Rule 1
```

**Rule 1 verified:** every endpoint tested for 401/403/404 across roles and locals; a guard test fails the build if any view skips role or local scoping; composite FKs make Postgres refuse cross-local rows.
**Rule 2 verified:** `docker compose exec api1 python manage.py prove_rule2 --username leader27 --password crewlink-demo-2026` fires 20 simultaneous Sends across both instances: 1,798 members, at most 1 push each. 111 tests run on real Postgres.

## TEST ACCOUNTS

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

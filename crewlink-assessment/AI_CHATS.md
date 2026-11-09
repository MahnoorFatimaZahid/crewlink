# AI conversations

AI was used as a technical mentor and engineering partner throughout this assessment,
not only as a code generator: requirements were worked through step by step, design
alternatives compared, and each stage reviewed before moving on.

| Tool | What it was used for | Link |
|---|---|---|
| Claude | Requirements (Part 0), design (Part A), backend (Rules 1 and 2, send path, sweeper, integrity checks), AI draft feature, nginx bonus, reviews of each stage | [Shared conversation](https://claude.ai/share/633896d2-df01-4eaf-96d7-ab530059f2dd) |

## How the conversation is organised

1. **Understanding the brief:** mandatory vs optional work, and the traps in Denise's message
2. **Part 0 · Requirements:** assumptions, concerns, questions for Denise
3. **Part A · Design:** data model, send path, Rule 1 and Rule 2, detection, diagram
4. **Build, stage by stage:** login and Rule 1 → send flow and Rule 2 → AI drafts → leadership screen → nginx with two API instances
5. **Verification:** tests run against real Postgres, live sends with simulated push failures, worker crash recovery, and the Rule 2 proof across instances

## Notable findings along the way

- **Fan-out could add members who joined after Send.** Fixed so fan-out only runs while the announcement is `queued`.
- **Duplicate-push detection was blind to crashes.** A worker killed mid-batch left no record of pushes it had sent. Fixed with an intent log written before each push.
- **The login rate limit could be bypassed** with a fake `X-Forwarded-For` header. Fixed in nginx (overwrite the header) and Django (`NUM_PROXIES`).

# Decisions
- dashboard/app.py: Phase 1 placeholder so all 4 compose services start; replaced in Phase 5
- scenarios/runner.py: --engine flag is parsed but only the plain engine exists until Phase 5b; scorecard call added in Phase 3
- gateway/store.py: approvals table has no status_code column, so response_json stores {"status_code", "response"}
- gateway/pipeline.py: compensation rows (step 13, ON only) are written in Phase 4 together with rollback
- gateway/app.py: GET /approvals/{id} only validates the bearer token (no envelope, no ownership check), as specified
- scenarios/attacks.py: RogueClient takes an optional httpx client (for tests) and exposes mint_jwt/send helpers
- scripts/dev_local.sh: sources .env (set -a) before the local URL/DB overrides so gateway and runner share ADMIN_TOKEN
- agents/admin.py: AdminClient raises AdminError (method, path, status code, body) on non-2xx instead of returning error JSON
- gateway/store.py: get_events returns the most recent <limit> events (ascending) when since_ts is absent or 0; with since_ts > 0 it returns the oldest events after it, so polling works

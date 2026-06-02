---
status: in-progress
branch: main
timestamp: 2026-05-21T18:15:17+03:00
files_modified: []
---

## Working on: Railway Deploy + Google Calendar Integration

### Summary

Deployed SYNC LAB multi-agent office (CrewAI + Claude Sonnet 4.6) to Railway. Added Google Calendar integration so the coordinator agent "Рядовой" can create calendar events via Telegram. All "Синкотека"/"Syncoteca" branding renamed to "SYNC LAB" throughout codebase. Push to `main` succeeded (commit `1c63e21`), Railway auto-deploy triggered.

### Decisions Made

- **Coordinator is direct Claude API, not CrewAI** — `bot.py` calls `anthropic.messages.create` with `coordinator.md` prompt directly. Calendar action added as JSON protocol: `{"action": "calendar", ...}` handled in `_dispatch_coordinator`.
- **Google Calendar auth** — `GOOGLE_TOKEN_JSON` env var holds full OAuth2 token JSON. `GoogleCalendarTool._get_service()` loads credentials at runtime. Falls back to mock if env var absent.
- **Coordinator name** — Changed from "Оля" → "Рядовой" (military theme).
- **Branding** — All "Синкотека"/"Syncoteca" → "SYNC LAB", Python class `SyncotecaCrew` preserved (was accidentally renamed, then fixed).
- **Git merge** — Remote had diverged history; merged with `--allow-unrelated-histories` + `--no-rebase`. Conflicts in `.gitignore` and `marina.md` resolved with `--ours`.

### Remaining Work

1. **CRITICAL — Rotate Anthropic API key**: Key from `CBAPI.txt` is in git history (commit `c85406655707c7fdde982fa8033d2f6dad10e06b`). Go to console.anthropic.com → create new key → update `ANTHROPIC_API_KEY` in Railway Variables → delete old key.
2. **Verify Railway deploy**: Check Railway Dashboard → Deployments tab — confirm build green after commit `1c63e21`.
3. **Add GOOGLE_TOKEN_JSON to Railway Variables** (if not done yet) — paste the JSON from local `.env` file.
4. **Test calendar via Telegram**: Send "Рядовой, поставь встречу 'Тест Railway' на завтра в 11:00"
5. **Cleanup untracked files**: `rm test_calendar.py get_google_token.py client_secret_*.json` (`.env.google_token` keep locally, already in `.gitignore`)

### Notes

- **SECURITY**: The screenshot "Secret allowed" from GitHub means GitHub unblocked the push — it does NOT mean the key is safe. The key is still in git history and must be rotated immediately at console.anthropic.com.
- Railway project: `invigorating-reprieve`, GitHub repo: `SyncLab2017/syncoteca-office`, auto-deploy on push to `main`.
- `GOOGLE_TOKEN_JSON` contains access_token + refresh_token + client_secret — never commit or share publicly.
- `{TODAY}` placeholder in `coordinator.md` is injected at runtime by `_get_coordinator_prompt()` in `bot.py`.
- Key files changed this session:
  - `src/syncoteca/tools/google_calendar_tool.py` (NEW)
  - `src/syncoteca/config/prompts/coordinator.md` (calendar action + {TODAY})
  - `src/syncoteca/bot.py` (calendar dispatch handler)
  - `src/syncoteca/crew.py` (GoogleCalendarTool added)
  - `pyproject.toml` (google-api-python-client deps)
  - All prompts/configs: Синкотека → SYNC LAB

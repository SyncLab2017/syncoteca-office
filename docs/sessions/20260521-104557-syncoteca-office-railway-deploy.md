---
status: in-progress
branch: clean_main2
timestamp: 2026-05-21T10:45:57+03:00
files_modified: []
---

## Working on: Syncoteca Office — Railway Deploy

### Summary

Multi-agent AI licensing office (CrewAI + Claude Sonnet 4.6) with FastAPI web server and Telegram bot (@DenisSharko_bot). Project is ready for Railway deployment: Dockerfile exists, `start.py` already has Railway env detection (`RAILWAY_ENVIRONMENT`). Repo on GitHub: SyncLab2017/syncoteca-office. Two commits in history — codebase is clean.

### Decisions Made

- **Stack**: CrewAI framework + `claude-sonnet-4-6` via Anthropic API
- **Entry point**: `python start.py` — launches FastAPI office (port from `$PORT` env) + Telegram bot in parallel threads
- **Dockerfile**: `python:3.11-slim`, installs via `pip install -e .`, copies `data/knowledge/` and `src/syncoteca/config/prompts/`
- **Railway env detection**: `start.py` binds to `0.0.0.0` when `RAILWAY_ENVIRONMENT` is set, else `127.0.0.1`
- **No `railway.toml`** yet — Railway can auto-detect Dockerfile, but explicit config recommended
- **7 agents**: coordinator, license_manager, content_manager, accountant, lawyer, biz_dev, developer
- **Database**: Supabase (PostgreSQL) via `SUPABASE_URL` + `SUPABASE_KEY` env vars

### Remaining Work

1. Add `railway.toml` to pin build config (Dockerfile, start command, healthcheck)
2. Set all required env vars in Railway dashboard: `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `SUPABASE_URL`, `SUPABASE_KEY`, `SERPER_API_KEY` (optional)
3. Deploy to Railway — push to GitHub triggers auto-deploy (if connected) or `railway up`
4. Verify: office web UI accessible at Railway public URL, Telegram bot responds
5. Set up Railway healthcheck on `GET /` or `/health` endpoint if not already present

### Notes

- Previous session was large — context was lost due to terminal restart. No `/context-save` was called during that session.
- `data/knowledge/` is copied into Docker image — update this if knowledge base grows large
- Telegram bot token env var name needs verification (check `src/syncoteca/bot.py` for exact var name used)
- Agents listed in CLAUDE.md use placeholder tools (`SearchTool`, `EmailDraftTool`, etc.) — verify these are actually implemented in `src/syncoteca/tools/`
- Branch: `clean_main2` (not `main`) — confirm this is the intended deploy branch

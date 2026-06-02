---
status: in-progress
branch: main
timestamp: 2026-05-22T14:29:43+03:00
files_modified: []
---

## Working on: Ekaterina — Sonnet, email+Asana pipeline working

### Summary

Syncoteca Office fully deployed on Railway (invigorating-reprieve). Resend email integration confirmed working (test email arrived at denis@synclab.pro). Asana workspace save fixed. Ekaterina upgraded from Haiku to Sonnet with more flexible prompting. Core pipeline — draft → send email → save to Asana — is functional.

### Decisions Made

- **Ekaterina upgraded to claude-sonnet-4-6** in `run_license_dialogue` (was haiku). Better JSON protocol adherence. File: `src/syncoteca/bot.py:181`.
- **Robust JSON extraction**: parser now tries multiple strategies — strip ```json blocks, find first JSON object in text, fallback to continue_dialogue. Fixes Katya returning JSON wrapped in text.
- **Ekaterina prompt rewritten** (ekaterina.md): less strict — draft with partial info, use `[уточнить]` for missing fields. Minimum to draft: track name + project type + brand/title. Max one question per turn.
- **Asana: no assignee** — removed `"assignee": "me"` which was causing tasks to land in My Tasks. Now uses workspace (`ASANA_WORKSPACE_ID = 331121027676371` for "Офис SYNC LAB") without project assignment.
- **send_email action = email + Asana** — bot already handles both in `_dispatch_license`. Katya's prompt now says this explicitly so she doesn't try to return both actions separately.
- **Resend API for email** (RESEND_API_KEY in Railway). Domain synclab.pro verified. Sends from denis@synclab.pro.

### Remaining Work

1. **Test full pipeline after deploy** — ask Катя for a real request, say «отправляй», confirm email arrives + Asana task created in workspace (not My Tasks)
2. **Asana: set proper project** — eventually link tasks to the right Asana project (ПРАВООБЛАДАТЕЛИ or a new project). For now tasks float in workspace.
3. **Cleanup local files** (before production):
   - `rm .env.google_token get_google_token.py test_calendar.py client_secret_*.json`
   - All contain OAuth credentials — do NOT commit
4. **Add SERPER_API_KEY** to Railway for full Google search (using DuckDuckGo fallback now)
5. **Rotate Anthropic API key** — key from `CBAPI.txt` in git history (commit c85406655707) is compromised. Create new at console.anthropic.com → update Railway Variables before production.

### Notes

- **Railway env vars set**: ANTHROPIC_API_KEY, ASANA_TOKEN, ASANA_WORKSPACE_ID=331121027676371, RESEND_API_KEY, GOOGLE_TOKEN_JSON, TELEGRAM_BOT_TOKEN
- **Asana workspace GID**: 331121027676371 (workspace "Офис SYNC LAB")
- **Resend domain**: synclab.pro verified, region us-east-1, 3 DNS records added in NetAngels (TXT DKIM `resend._domainkey`, MX `send` → feedback-smtp.us-east-1.amazonses.com, TXT SPF `send`)
- **Architecture**: Coordinator (Рядовой) = direct Claude Haiku API with JSON actions {route, calendar, search, reply}. Ekaterina = direct Claude Sonnet API with JSON actions {continue_dialogue, draft_ready, save_to_asana, send_email}. Agents not used via CrewAI in Telegram bot — all direct API calls.
- **Model split**: coordinator=haiku (cheap, routing only), ekaterina=sonnet (complex licence workflow)
- **Committed but not yet tested**: `cdc2044` — Sonnet + flexible prompt. Need to verify in Telegram after Railway deploys.
- **Local untracked (do not commit)**: `.env.google_token`, `client_secret_*.json`, `get_google_token.py`, `test_calendar.py`

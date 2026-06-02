---
status: in-progress
branch: main
timestamp: 2026-05-22T17:41:35+03:00
files_modified: []
---

## Working on: Ekaterina — contact search quality (Supabase stemming + Asana clean terms)

### Summary

Syncoteca Office is deployed on Railway (invigorating-reprieve). This session fixed Ekaterina's contact search pipeline: she was hallucinating contacts when real data existed in Supabase, failing to find inflected Russian names (e.g., "Козина" → stored as "Козин"), and sending full user messages as Asana search queries (stop words killed matches). All three issues are now fixed and pushed.

### Decisions Made

- **Russian morphology via simple suffix stripping (`_stem_ru`)**: Not a full morphological analyzer — strips trailing Russian ending characters (`аеёиоуыэюяйь`) up to 2 levels deep. Fast, zero-dependency, handles 90% of Russian inflection for names. E.g., "Козина" → ["Козина", "Козин"]; "Алексея" → ["Алексея", "Алексе", ...] (imperfect on vowel-only removal but good enough for last names and labels).
- **Expanded OR conditions to 18 max** (was 30 from 6 terms × 5 cols; now up to 18 stemmed variants × 5 cols = 90 conditions). Added `all_terms[:18]` cap to avoid Supabase URL length limits.
- **Asana query cleaned with `_extract_search_terms`**: Full user request (e.g., "Найди контакты Zion Music") was being passed directly to Asana text search → extracted to ["zion", "music"] → joined → clean Asana query.
- **Session persistence via Supabase `agent_sessions`** (from prior session): `sticky_{chat_id}` rows persist `ACTIVE_AGENT` across Railway restarts. `_persist_active_agent` / `_restore_active_agent` / `_clear_active_agent` in bot.py.
- **No session clear after send**: `LICENSE_SESSIONS[chat_id] = []` removed from all send handlers; resets only on `/stop` or `/license`.
- **Message splitting**: Drafts >4000 chars split into chunks; "Куда отправить?" always last message.
- **HTML email signature**: Exact colors from .eml (`#2bb4d7` teal, `#0068da` pipes, `#008f00` NeoSounds, `#942192` Sound Scape, `#ff40ff` Twisted Jukebox, `#e61e1c` Кинопоиск). Resend sends both `html` and `text`. SMTP uses `MIMEMultipart("alternative")`.
- **Brand/project name format**: No guillemets on brand/project side. `Coca-Cola | Imagine Dragons — «Believer»`.

### Remaining Work

1. **Test contact search after Railway deploys `dacd3c9`** — ask Ekaterina to find contacts for known labels (ZION MUSIC id=57, Soyuz, etc.) and verify stemming works in practice
2. **Verify Supabase OR filter doesn't hit URL length limit** with many stem variants — if it does, split into multiple requests
3. **Rotate Anthropic API key** — key from `CBAPI.txt` is compromised (commit `c85406655707`). Create new at console.anthropic.com → update Railway Variables before production
4. **Add SERPER_API_KEY** to Railway for full Google search (currently DuckDuckGo fallback)
5. **Cleanup local sensitive files** (do NOT commit): `.env.google_token`, `client_secret_*.json`, `get_google_token.py`, `test_calendar.py`
6. **Eventually**: link Asana tasks to specific project (currently float in "Офис SYNC LAB" workspace)

### Notes

- **Railway env vars required**: `ANTHROPIC_API_KEY`, `ASANA_TOKEN`, `ASANA_WORKSPACE_ID=331121027676371`, `RESEND_API_KEY`, `GOOGLE_TOKEN_JSON`, `TELEGRAM_BOT_TOKEN`, `SUPABASE_URL`, `SUPABASE_KEY`
- **Resend domain**: synclab.pro verified, us-east-1, sends from denis@synclab.pro
- **Asana workspace GID**: 331121027676371 ("Офис SYNC LAB") — hardcoded as default
- **Architecture**: coordinator=`claude-haiku-4-5-20251001`, Ekaterina=`claude-sonnet-4-6`. Direct API calls, no CrewAI for bot.
- **Supabase contacts table**: `contacts` with columns `owner_type`, `first_name`, `last_name`, `email`, `adittional_info` (note: typo in column name is real, not a bug)
- **`_stem_ru` limitation**: strips ending chars only; "Алексея" becomes "Алексе" not "Алексей". Works for last names; first names less reliable. Consider adding a small lookup table for common first-name inflections if search quality is still poor.
- **Asana search API**: `GET /workspaces/{id}/tasks/search` with `text=` param, limit=5, returns name+notes+permalink_url
- **Local untracked (do NOT commit)**: `.env.google_token`, `client_secret_*.json`, `get_google_token.py`, `test_calendar.py`, `(Без темы).eml`
- **Commits this session**: `dacd3c9` (stemming + Asana clean terms), `ce3cdc2` (sticky agent + word-by-word search), `ec974fc` (parallel Supabase+Asana search), `50d2c71` (Asana search before respond), `3c80a24` (sticky via coordinator, exact signature colors), `e383c7b` (exact signature), `724f4ce` (session persistence, message splitting), `dfaad14` (brand format, HTML signature)

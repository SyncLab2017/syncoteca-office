---
status: in-progress
branch: main
timestamp: 2026-05-26T19:14:33+03:00
files_modified:
  - src/syncoteca/config/prompts/coordinator.md
---

## Working on: Tavily + Asana tasks + Calendar fixes session

### Summary

Syncoteca Office on Railway (invigorating-reprieve). Session shipped multiple features and fixes: Asana task creation via coordinator, Google Calendar attendee bug fix (root cause: LLM hallucinating emails), Ekaterina rights holder contacts auto-fill from db_context, Tavily web search integration for Ekaterina, stop words expansion for Russian search.

### Decisions Made

- **Coordinator Asana task creation**: new `asana_task` action in coordinator.md + handler in `_dispatch_coordinator`. User says "занеси в асану X" → task created, assigned denis@synclab.pro, due today.
- **Calendar attendee bug (root fix)**: LLM was hallucinating emails for company names → Google Calendar 400. Fixed in `_dispatch_coordinator`: attendees extracted via regex from RAW USER TEXT only, LLM-generated attendees ignored entirely. `valid_attendees = re.findall(r'[a-zA-Z0-9_.+\-]+@[a-zA-Z0-9\-]+\.[a-zA-Z]{2,}', text)`.
- **Coordinator JSON multi-output parser**: Haiku sometimes outputs two JSON objects (reply + search). Parser now extracts all JSON objects, sorts by priority (asana_task > calendar > search > route > reply), returns best.
- **Ekaterina rights holder contacts**: ПРАВИЛО КОНТАКТОВ added to ekaterina.md. When Supabase/Asana returns contacts for the artist/label, Ekaterina fills `📌 Правообладатель` and `📧 Контакт` instead of `[уточнить]`. Footer added to both letter templates.
- **Tavily integration**: `TavilySearchTool` created in `src/syncoteca/tools/tavily_search_tool.py`. Called in `_dispatch_license` in parallel with Supabase+Asana. Results injected as `[ВЕБ-ПОИСК]` block before LLM. Requires `TAVILY_API_KEY` in Railway.
- **Stop words expanded**: `контактов`, `контакте`, `контакту`, `покажи`, `скажи`, `поиск`, `запрос` added to `_SEARCH_STOP_WORDS`.
- **Anthropic API key rotated**: old key from `CBAPI.txt` (commit `c85406655707`) revoked. New key set in Railway manually by Denis.
- **`API Tavily.txt` and `CBAPI.txt` added to .gitignore**.

### Remaining Work

1. **Add `TAVILY_API_KEY` to Railway** — key: `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` → Railway → invigorating-reprieve → Variables (if not already done)
2. **Test Tavily in prod** — send a license request to Екатерина about a known brand (e.g. Domestos, Nike) and verify she has web context
3. **Test Asana task creation** — send "занеси в асану: X" to coordinator and verify task appears in Asana
4. **Test Calendar fix** — "встреча с Иваном завтра в 17:00" should create event without attendee error
5. **Add `SERPER_API_KEY`** to Railway for coordinator web search (Google quality vs DuckDuckGo fallback)
6. **Uncommitted change**: `coordinator.md` has local modification — check `git diff src/syncoteca/config/prompts/coordinator.md` and commit if needed
7. **Cleanup local files** (do NOT commit): `.env.google_token`, `client_secret_*.json`, `get_google_token.py`, `test_calendar.py`

### Notes

- **Railway env vars required**: `ANTHROPIC_API_KEY` (rotated), `ASANA_TOKEN`, `ASANA_WORKSPACE_ID=331121027676371`, `RESEND_API_KEY`, `GOOGLE_TOKEN_JSON`, `TELEGRAM_BOT_TOKEN`, `SUPABASE_URL`, `SUPABASE_KEY`, `TAVILY_API_KEY` (new)
- **Architecture**: coordinator=`claude-haiku-4-5-20251001`, Ekaterina=`claude-sonnet-4-6`. Direct API calls.
- **Tavily key file**: `API Tavily.txt` in project root — gitignored, do NOT commit
- **Supabase column typo**: `adittional_info` is real column name, not a bug
- **Commits this session**: `7d9f2fb` (Tavily), `b2bfe37` (calendar attendees root fix), `c256a2f` (email regex), `7dae6da` (Ekaterina contacts), `65d11b7` (JSON parser + prompt), `087a8f6` (Asana tasks), `542674d` (stop words)
- **Coordinator.md has uncommitted local change** — needs review before next session

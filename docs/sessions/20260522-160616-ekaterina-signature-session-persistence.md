---
status: in-progress
branch: main
timestamp: 2026-05-22T16:06:16+03:00
files_modified: []
---

## Working on: Ekaterina — signature, session persistence, message splitting

### Summary

Syncoteca Office is deployed on Railway (invigorating-reprieve). Core pipeline (draft → email/Asana) is fully functional. This session polished Ekaterina's email output and fixed several UX bugs: brand name format, HTML signature, Telegram message overflow, and session reset after send.

### Decisions Made

- **Brand/project name format changed**: removed guillemets from brand/project side. Format is now `Coca-Cola | Imagine Dragons — «Believer»` (song name keeps quotes, brand does not). Updated in both `ekaterina.md` and the template blocks.
- **HTML email signature added** (`email_tool.py`): Resend API sends `html` + `text` fields. Signature matches Denis's real signature exactly — teal `#3DA8B4` for name/contact, pink `#CC3B7A` for Twisted Jukebox, orange `#B86D2A` for Кинопоиск. No book image. All 9 links with real URLs.
- **Session not cleared after send**: removed `LICENSE_SESSIONS[chat_id] = []` from `save_to_asana`, `send_email`, `send_both` handlers. Session now only resets on `/stop` or `/license` (explicit agent switch). User stays with Ekaterina after sending.
- **Message splitting for Telegram 4096 char limit**: `draft_ready` action now splits long drafts into multiple messages; "Куда отправить?" prompt always sent as the last message.
- **`save_to_asana` status message** changed from showing full_text (which caused `Message_too_long`) to short `"📋 Сохраняю в Asana: {subject}…"`.

### Remaining Work

1. **Test HTML signature in real email** — after Railway deploys `e383c7b`, send a test letter and confirm rendering matches the screenshot
2. **Rotate Anthropic API key** — key from `CBAPI.txt` is compromised (commit c85406655707). Create new at console.anthropic.com → update Railway Variables before production
3. **Add SERPER_API_KEY** to Railway for full Google search (currently DuckDuckGo fallback)
4. **Cleanup local sensitive files** (do NOT commit): `.env.google_token`, `client_secret_*.json`, `get_google_token.py`, `test_calendar.py`
5. **Eventually**: link Asana tasks to a specific project (currently float in "Офис SYNC LAB" workspace)

### Notes

- **Railway env vars**: ANTHROPIC_API_KEY, ASANA_TOKEN, ASANA_WORKSPACE_ID=331121027676371, RESEND_API_KEY, GOOGLE_TOKEN_JSON, TELEGRAM_BOT_TOKEN
- **Resend domain**: synclab.pro verified, us-east-1, sends from denis@synclab.pro
- **Asana workspace GID**: 331121027676371 ("Офис SYNC LAB") — hardcoded as default in `save_to_asana()`
- **Signature link URLs**:
  - Sync Lab → https://synclab.sourceaudio.com/#!artists
  - NeoSounds Ltd → https://www.neosounds.com/
  - Sound Scape → https://soundscape.io/
  - Twisted Jukebox → https://www.twistedjukebox.com/
  - Сайт → https://synclab.pro/
  - Кинопоиск → https://www.kinopoisk.ru/name/6269377/
  - Отзыв → https://goo.gl/forms/iYHf5wKi5CBYHWtl1
  - Feedback here → x-webdoc://6A875865-DF45-48B6-8AC1-CC74F2764D5F (local mac doc)
  - Контур.Диадок → https://kontur.ru/diadoc
- **Architecture**: coordinator=haiku (routing), ekaterina=sonnet (licensing). All direct API calls, no CrewAI for bot.
- **Local untracked (do NOT commit)**: `.env.google_token`, `client_secret_*.json`, `get_google_token.py`, `test_calendar.py`
- **Commits this session**: `dfaad14`, `724f4ce`, `e383c7b`

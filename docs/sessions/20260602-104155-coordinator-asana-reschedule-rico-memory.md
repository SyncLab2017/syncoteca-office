---
status: in-progress
branch: main
timestamp: 2026-06-02T10:41:55+03:00
files_modified:
  - src/syncoteca/bot.py
  - src/syncoteca/config/prompts/coordinator.md
---

## Working on: Coordinator Asana Reschedule + Rico Memory

### Summary

Extended the multi-agent Telegram bot (Syncoteca Office) deployed on Railway (`invigorating-reprieve`). Main work this session: voice-driven Asana task rescheduling for Coordinator (Рядовой), Rico memory compression to 30 turns, year-only track search fix. All changes shipped to GitHub (auto-deploy via Railway).

### Decisions Made

- **Rico session memory 10→30 turns**: Old limit was `history[-20:]` (10 pairs). Each user message injects large track/contact data blocks. Fix: compress data blocks (`[ТРЕКИ ИЗ БАЗЫ...]`, `[КОНТАКТЫ...]`) in all messages older than last 4 (= last 2 pairs), keep full context only for current work. Limit raised to `history[-60:]`.

- **Year-only search fix**: "Песни 1983 года" returned 1 result because "1983" was searched in title/artist/album/label fields (74 false matches), then Python post-filtered to 1. Fix: detect `year_only_search` (year detected + no non-year text terms) → query `release_date=ilike.*1983*` directly. Added year words to `_TRACK_SEARCH_NOISE`: "год/года/году/годов/годом/годах/лет" + command words "выбери/найди/поищи/подбери/отбери".

- **Asana task reschedule via voice**: Added `find_asana_task_by_name()` (text search in workspace) and `update_asana_task_due()` (PUT /tasks/{gid}). New `_is_reschedule_intent()` checks for rescue verbs (сдвинь/перенеси) + "задач" — runs BEFORE briefing check to prevent intercept. `parse_reschedule_intent()` regex extracts task_name ("задачу X на Y" pattern) and date. `_parse_new_due()` handles: завтра/послезавтра/сегодня, weekday names, "10 июня", "через N дней", ISO dates.

- **Briefing intercept bug (CRITICAL bug pattern)**: `_is_briefing_intent` triggered on ANY message containing "задач" (keyword in `_BRIEFING_KEYWORDS`). Both "покажи задачи на завтра" and "сдвинь задачу на завтра" and "поставь задачу в асану" all matched. Fixed in two passes:
  1. Reschedule checked BEFORE briefing (both contain "задач" + date words)
  2. Added `_TASK_CREATION_VERBS` exclusion: if message has creation verb (поставь/создай/добавь/занеси/etc) + "задач" → not briefing, falls through to LLM

- **Robustness fixes for natural speech**: 
  - "завтрашний день" → `_parse_new_due` now uses `startswith("завтра")` not exact match
  - Trailing period in "на завтра." → `rstrip(".,!?;:")`
  - "мне", "пожалуйста", "давай" in task_name → `_TASK_NAME_NOISE` stripped
  - "под названием X" prefix → `_TASK_PREFIX_STRIP` strips from task_name
  - Quoted task names `"финансы Киргизия"` → `_extract_quoted_strings` used

- **Confirmation message**: After reschedule, shows only `✅ Задачу «Финансы Киргизия» сдвинул на завтра, 3 июня (среда).` — no full briefing dump (user confirmed this is correct)

- **Python 3.13 safety**: Always run `python3 -c "import ast; ast.parse(...)"` before every commit. Unicode confusables (U+201C/201D) crash Railway.

- **HTTPS push only**: SSH broken. Always `git push https://github.com/SyncLab2017/syncoteca-office.git main`.

### Remaining Work

1. **Test reschedule end-to-end** after Railway deploys: say "Сдвинь задачу финансы Киргизия на завтра" — should find task, update due_on, confirm with Russian date
2. **Test creation commands** no longer show briefing: "Поставь мне задачу на 13 июня в Асане, подпиши Альфа-мания"
3. **Test year search**: "Песни 1983 года" — should return tracks with release_date containing "1983"
4. **Verify Rico memory**: long session (>10 turns) — Rico should remember early context
5. **Other agents** (biz_dev, lawyer, accountant, content_manager): not yet improved
6. **Coordinator voice flow** (from 20260529 checkpoint): still needs full end-to-end test

### Notes

- **Railway**: `invigorating-reprieve`, auto-deploys on push to main, ~2-3 min
- **GitHub**: `SyncLab2017/syncoteca-office`, HTTPS push only
- **Latest commits** (all on main, deployed):
  - `64abf68` fix(coordinator): creation commands no longer trigger briefing
  - `f40f6ef` fix(coordinator): no briefing dump after reschedule
  - `689e3c9` fix(coordinator): robust reschedule parsing for natural speech
  - `e336f1c` fix(coordinator): human-readable reschedule confirmation
  - `88c3a61` fix(coordinator): reschedule bypasses briefing intercept
  - `c1b5d46` feat(coordinator): reschedule Asana tasks by voice command
  - `2c4dfb8` feat(rico): extend session memory to 30 turns
  - `ddfec19` fix(search): year-only queries use release_date directly
- **Asana team**: Denis=denis@synclab.pro (default), Катя=kate@synclab.pro, Саша=alexa.sp@yandex.ru
- **Supabase**: `zpuvorqtdvjbmqmjgtll`, `release_date` column is TEXT mixed format ("2015" / "23.04.2015 (2015)")
- **_INJECT_BLOCK_RE**: regex for compressing old history — strips `[ТРЕКИ ИЗ БАЗЫ...]`, `[КОНТАКТЫ...]`, `[ИСТОРИЯ СДЕЛОК...]`, `[КОНТАКТЫ ПРАВООБЛАДАТЕЛЕЙ...]`
- **_BRIEFING_KEYWORDS** intercept risk: any new feature adding "задач/дел/план" to messages must check if briefing detection will intercept it — use `_TASK_CREATION_VERBS` or a similar exclusion
- **Env vars on Railway**: SUPABASE_URL, SUPABASE_KEY, ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, ASANA_TOKEN, ASANA_WORKSPACE_GID, ASANA_PROJECT_GID, ASANA_NAME_EKATERINA, ASANA_NAME_ALEXANDRA, TELEGRAM_OWNER_ID

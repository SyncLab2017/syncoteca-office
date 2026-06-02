---
status: in-progress
branch: main
timestamp: 2026-05-29T10:17:22+03:00
files_modified: []
---

## Working on: Coordinator (Рядовой) — Asana Briefing via Voice

### Summary

Syncoteca Office — multi-agent music sync licensing bot (Python, CrewAI, Telegram).
This session improved the Coordinator agent (Рядовой): it now handles all Asana task
queries directly (without routing to Рико), supports date ranges and per-person filters,
and responds correctly to voice messages in Russian and English.

### Decisions Made

- **Briefing intent detection** — `_is_briefing_intent` now triggers on task keywords alone (no "сегодня" required). Also triggers on person-name scope words combined with "задач/дел/план".
- **Person filter** — replaced `filter_me: bool` with `filter_person: str | None` supporting `None` (all), `"me"` (current user GID via `/users/me`), `"ekaterina"`, `"alexandra"`.
- **Name matching** — "me" uses Asana API `assignee.any` GID param. Ekaterina/Alexandra use post-fetch substring match against env vars `ASANA_NAME_EKATERINA` / `ASANA_NAME_ALEXANDRA`; fallback defaults are `"екатерина"` / `"александра"`.
- **English name variants** — Kate/Katya/Katie/Ekaterina → ekaterina; Alex/Sasha/Alexandra/Alexa → alexandra; My/mine → me.
- **Unassigned tasks hidden** — tasks without `assignee.name` filtered out in `format_morning_briefing` for both today and week/tomorrow views.
- **/stop clears teach mode** — `TEACH_SESSIONS.pop(chat_id)` added to `handle_stop`; previously bot stayed stuck in Рико teach mode.
- **`morning_briefing_job` + `handle_briefing`** — both fixed from `False` → `None` for `filter_person`.
- **Push via HTTPS** — SSH broken in this env; using `https://github.com/SyncLab2017/syncoteca-office.git`.

### Remaining Work

1. Test voice flow end-to-end after Railway deploys latest commits (62fdf04)
2. Set Railway env vars if not already set:
   - `ASANA_NAME_EKATERINA` — name fragment as it appears in Asana (e.g. "Екатерина")
   - `ASANA_NAME_ALEXANDRA` — name fragment as it appears in Asana (e.g. "Александра")
3. Continue improving other agents (after coordinator is stable)

### Notes

- Railway at `invigorating-reprieve`, auto-deploys on push to main
- GitHub: `SyncLab2017/syncoteca-office` (HTTPS push only in this env — SSH drops)
- Coordinator prompt loaded from `src/syncoteca/config/prompts/coordinator.md`
- `_asana_me_gid_cache` caches `/users/me` GID — cleared on Railway restart
- Colleague mapping: Екатерина = Kate, Александра = Sasha/Alex (female)
- Teaching mode bug: `/stop` didn't clear TEACH_SESSIONS → all messages went to Рико memory. Fixed in commit 666170f.
- Morning briefing scheduled daily 09:00 Moscow time via `job_queue.run_daily`

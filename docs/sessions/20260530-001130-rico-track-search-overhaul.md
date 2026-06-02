---
status: in-progress
branch: main
timestamp: 2026-05-30T00:11:30+03:00
files_modified:
  - src/syncoteca/bot.py
  - src/syncoteca/config/prompts/rico.md
---

## Working on: Rico Track Search Overhaul

### Summary

Full overhaul of `search_supabase_tracks` in `bot.py` — switched from PostgreSQL FTS (broken on PostgREST text columns) to ilike-based OR search with noise filtering, transliteration, phrase matching, label search, year range filtering, and context size guards. Rico prompt updated for concise responses. All changes deployed to Railway (invigorating-reprieve) via GitHub push.

### Decisions Made

- **FTS → ilike**: PostgREST `plfts` on TEXT column uses default tsconfig, not 'russian' → always 0 results. GIN indexes exist but unusable via PostgREST on plain TEXT columns. Switched to `ilike.*term*` per-term OR conditions.
- **Per-term OR, not joined AND**: `plainto_tsquery('russian', 'сколько однажды')` requires BOTH words → 0 results. Per-term: each term gets independent conditions across title/artist/album/label.
- **Phrase-first**: multi-word terms (e.g. "Агата Кристи") get a full-phrase `artist.ilike.*агата кристи*` condition added first before per-word fallbacks. Prevents "кристи" matching Кристина Орбакайте in first 30 slots.
- **Cyrillic→Latin transliteration**: "НАутилуса" → stem "наутилус" → translit "nautilus" → `artist.ilike.*nautilus*` → matches "Nautilus Pompilius" (stored in Latin). Applied per-term to artist/title/label.
- **Label column in OR**: non-quoted queries now search label field. Quoted queries ("Мелодия") do NOT search label — avoids flooding with Фирма Мелодия tracks.
- **Year range filter**: extracted via regex from query, applied as Python post-filter (not PostgREST — release_date is TEXT with mixed formats "2015" / "23.04.2015 (2015)"). Skipped for quoted-string queries to avoid "трек «1984»" false filtering.
- **limit 30→200**: covers full artist catalogs (e.g. Агата Кристи has 164 tracks).
- **Context size guard**: >30 results → compact format (title+artist+label+year only). ≤30 results → full details (authors, link). Prevents 100KB+ context blocks.
- **link field shown**: was SELECTed but never rendered. Now shown as "| Ссылка: URL" in detailed mode.
- **release_date shown**: year extracted from any format, shown as "| Год: YYYY".
- **Rico prompt format rules**: added strict concise-response block at top of rico.md — no "Отличный вопрос" filler, max 3–5 lines per info response.
- **HTTPS push only**: SSH broken in this env. Always `git push https://github.com/SyncLab2017/syncoteca-office.git main`.
- **Python 3.13 safety rule**: always run `python3 -c "import ast; ast.parse(open('src/syncoteca/bot.py').read()); print('SYNTAX OK')"` before every commit. Python 3.13 rejects Unicode confusable chars (U+201C/201D) anywhere in source — use `chr(0x201C)` instead.

### Remaining Work

1. **Test all filters end-to-end after Railway deploys**: label search (Lotus Music), year range (Фирма Мелодия 1970-1974), transliteration (Наутилус), phrase match (Агата Кристи)
2. **Monitor Rico context quality**: with 200-track limit, check if Rico handles large catalogs without hallucinating counts or omitting tracks
3. **Coordinator agent**: still needs testing for voice flow (per previous session's checkpoint 20260529-101722)
4. **Other agents** (biz_dev, lawyer, accountant, content_manager): not yet improved — focus after search is stable

### Notes

- **DB facts**: 164 Агата Кристи tracks (139 solo + 25 collaborative), 121 Nautilus Pompilius, 72 Лев Лещенко, 11452 total tracks
- **Supabase project**: `zpuvorqtdvjbmqmjgtll`, key in `.env`
- **release_date column is TEXT**, mixed formats: bare "2015" and "23.04.2015 (2015)" — regex extraction works, gte/lte PostgREST doesn't
- **GIN indexes exist** (`idx_tracks_title_gin`, `idx_tracks_artist_gin`, `idx_tracks_album_gin`) but unused — PostgREST FTS broken on TEXT columns. Keep indexes for future tsvector column migration.
- **Railway**: `invigorating-reprieve`, auto-deploys on push to main. ~2-3 min deploy time.
- **_TRACK_SEARCH_NOISE** contains: noise/stop words + track meta-words (трек/песня/музыка) + catalog query words (какие/тебя/базе/каталоге) + count words (сколько/много/количество)
- **Asana team**: Denis=denis@synclab.pro (default), Катя=kate@synclab.pro, Саша=alexa.sp@yandex.ru
- **Env vars needed on Railway**: SUPABASE_URL, SUPABASE_KEY, ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, ASANA_TOKEN, ASANA_WORKSPACE_GID, ASANA_PROJECT_GID, ASANA_NAME_EKATERINA, ASANA_NAME_ALEXANDRA

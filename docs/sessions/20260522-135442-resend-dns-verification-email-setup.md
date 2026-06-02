---
status: in-progress
branch: main
timestamp: 2026-05-22T13:54:42+03:00
files_modified: []
---

## Working on: Resend DNS verification + email setup for Ekaterina

### Summary

Syncoteca Office multi-agent system is deployed on Railway (invigorating-reprieve). All major features are live: Google Calendar (Рядовой), web search (Serper/DuckDuckGo), Asana search, and Ekaterina's email drafting. The final step is verifying synclab.pro domain in Resend so Ekaterina can send real emails to rights holders from denis@synclab.pro.

### Decisions Made

- **Resend API over SMTP**: Railway blocks outbound SMTP port 587 (`[Errno 101] Network is unreachable`). Switched to Resend HTTP API (HTTPS port 443, never blocked). Code in `src/syncoteca/tools/email_tool.py` — tries `RESEND_API_KEY` first, falls back to SMTP.
- **DNS in NetAngels**: synclab.pro nameservers point to NetAngels (not GoDaddy, which is just the registrar). All DNS changes go through NetAngels panel.
- **send_email action in Ekaterina**: `bot.py` `_dispatch_license` handles `send_email` action — calls `EmailDraftTool._run(send=True)` via Resend, then saves to Asana.
- **Resend account**: registered as denisfm133 (denisfm133@gmail.com), domain synclab.pro added, region us-east-1.

### Remaining Work

1. **Wait for Resend domain verification** — DNS records added in NetAngels, status "Verifying domain" (as of 13:52). Refresh Resend dashboard in ~10 min; should turn "Verified".
2. **Get RESEND_API_KEY** from Resend → Settings → API Keys → create new key
3. **Add RESEND_API_KEY to Railway Variables** (Railway dashboard → invigorating-reprieve → Variables)
4. **Test email via Telegram** — ask Катя to send a license request, confirm email arrives at target address
5. **Cleanup local files** (not urgent, before production):
   - `rm .env.google_token get_google_token.py test_calendar.py client_secret_*.json`
   - These contain OAuth credentials and should NOT be committed
6. **Add SERPER_API_KEY** to Railway for full Google search (currently using DuckDuckGo fallback — functional but limited)
7. **Rotate Anthropic API key** — old key from `CBAPI.txt` was committed in git history (commit c85406655707), is compromised. Create new key at console.anthropic.com, update Railway Variables before production.

### Notes

- **Git history has compromised Anthropic API key** (commit c85406655707 / `CBAPI.txt`). GitHub flagged it as "Secret detected". Do NOT use this key in production. Must rotate.
- **GOOGLE_TOKEN_JSON** contains access_token + refresh_token + client_secret — already in Railway Variables, never commit.
- **Resend DNS records added** (3 records in NetAngels):
  - TXT `resend._domainkey` → DKIM key (long p= value) — Status: Pending
  - MX `send` → `feedback-smtp.us-east-1.amazonses.com`, priority 10
  - TXT `send` → `v=spf1 include:amazonses.com ~all`
- **NetAngels quirk**: MX record form requires "Имя почтового сервера" (hostname) but also shows "IP-адрес" field — must leave IP blank for external hosts, otherwise error "Нельзя указывать IP-адрес для хоста, не находящегося в домене".
- **Deployed commits** (all on main, Railway auto-deploys):
  - `41af490` — Resend API email
  - `27df10e` — Ekaterina upgrade (Asana search, email send)
  - `653805d` — web search coordinator
  - `7c168f5` — railway.toml pip timeout fix
  - `9d9f0e4` — Google Calendar + Рядовой rename
- **Architecture**: FastAPI + Telegram bot in parallel threads. Coordinator (Рядовой) uses direct Claude API calls (not CrewAI) with JSON action protocol. Agents use CrewAI with claude-haiku-4-5-20251001, license_manager uses claude-sonnet-4-6.
- **Local untracked files to clean up**: `.env.google_token`, `client_secret_*.json`, `get_google_token.py`, `test_calendar.py` — sensitive credentials, do not commit.

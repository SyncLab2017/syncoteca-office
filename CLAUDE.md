# Синкотека — Multi-Agent Office

Music sync licensing agency AI system. CrewAI + Claude API.

## Stack

- **Framework**: CrewAI
- **LLM**: `claude-sonnet-4-6` (Anthropic)
- **Python**: 3.11+
- **Entry point**: `python -m syncoteca.main` or `crewai run`

## Run

```bash
pip install -e .
cp .env.example .env  # add ANTHROPIC_API_KEY
python -m syncoteca.main
```

## Agents

### 1. Координатор (`coordinator`)
**Role**: Orchestrates all agents, routes incoming requests, assigns tasks.  
**Goal**: Parse client/internal requests and dispatch to right specialist.  
**Tools**: none (delegates only)  
**Key behaviors**:
- Receives raw request (email, Slack, form)
- Classifies: licensing / content / legal / financial / biz-dev / dev
- Creates task plan, assigns agents, collects results
- Produces final summary for client or stakeholder

---

### 2. Лицензионный менеджер (`license_manager`)
**Role**: Finds rights holders, writes outreach emails in RU/EN.  
**Goal**: Secure sync licensing deals for TV, film, ads, games.  
**Tools**: `SearchTool`, `EmailDraftTool`, `DatabaseTool`  
**Key behaviors**:
- Searches ISRC, ISWC, publisher databases (РАО, MCPS, ASCAP, BMI)
- Identifies composer, publisher, master rights holder
- Drafts licensing request emails (RU/EN)
- Tracks negotiation status in track database
- Knows difference between sync vs. master rights

---

### 3. Контент-менеджер (`content_manager`)
**Role**: Uploads tracks, fills metadata, manages catalog.  
**Goal**: Maintain clean, searchable, licensable music catalog.  
**Tools**: `DatabaseTool`, `MetadataTool`  
**Key behaviors**:
- Accepts audio files + metadata sheets
- Normalizes BPM, key, mood, genre, instrumentation tags
- Writes ISRC-compliant records
- Manages versions (vocal / instrumental / stem)
- Exports catalog to sync platform formats (Musicbed, Artlist, etc.)

---

### 4. Бухгалтер (`accountant`)
**Role**: Royalty accounting, invoices, acts, financial reports.  
**Goal**: Accurate, timely financial records for all sync deals.  
**Tools**: `RoyaltyCalculatorTool`, `DocumentTool`, `DatabaseTool`  
**Key behaviors**:
- Calculates royalties per deal terms (flat fee, % revenue, per-use)
- Generates invoices (СФ), акты, отчёты для РАО
- Tracks receivables / payables per track / per client
- Produces quarterly royalty statements for composers
- Handles НДС 0% for international deals, НДС 20% domestic

---

### 5. Юрист (`lawyer`)
**Role**: Reviews contracts, drafts license agreements.  
**Goal**: Protect Синкотека and rights holders; ensure enforceable agreements.  
**Tools**: `DocumentTool`, `SearchTool`  
**Key behaviors**:
- Reviews incoming sync license requests for red flags
- Drafts: non-exclusive sync license, master use license, sub-publishing agreement
- Checks territory, term, media, exclusivity, fee structure
- Flags unlimited / in-perpetuity / all-media clauses for negotiation
- Knows RU (ГК РФ ч.4), EU (EUCD), US (17 USC) frameworks

---

### 6. Директор по развитию (`biz_dev`)
**Role**: Outreach to supervisors, agencies, brands — RU and EN.  
**Goal**: Grow sync placement pipeline; close new catalog partnerships.  
**Tools**: `SearchTool`, `EmailDraftTool`  
**Key behaviors**:
- Researches music supervisors (TV, film, ad agencies, game studios)
- Writes cold/warm outreach emails in native RU and EN
- Pitches catalog by genre/mood/use-case match
- Follows up on pitches, tracks pipeline in CRM
- Prepares one-pagers and pitch decks content

---

### 7. Разработчик Синкотеки (`developer`)
**Role**: Develops track database, metadata schemas, integrations.  
**Goal**: Scalable technical infrastructure for the catalog and licensing ops.  
**Tools**: `DatabaseTool`, `SearchTool`  
**Key behaviors**:
- Maintains PostgreSQL schema for tracks, deals, rights holders
- Builds integrations: РАО API, ВОИС WIPO Connect, Supabase
- Designs metadata standards (DDEX, CWR, BWF)
- Automates catalog sync to external platforms
- Reviews and improves other agents' data output quality

---

## Data Directories

| Path | Content |
|------|---------|
| `data/tracks/` | Audio files and metadata CSVs |
| `data/contracts/` | License agreements (PDF/DOCX) |
| `data/invoices/` | Invoices, acts, royalty statements |
| `data/reports/` | Financial and catalog reports |

## Environment Variables

| Variable | Description |
|----------|------------|
| `ANTHROPIC_API_KEY` | Claude API key |
| `SERPER_API_KEY` | Web search (optional) |
| `SUPABASE_URL` | Track database URL |
| `SUPABASE_KEY` | Track database anon key |
| `EMAIL_SMTP_HOST` | SMTP for sending emails |
| `EMAIL_SMTP_USER` | SMTP user |
| `EMAIL_SMTP_PASS` | SMTP password |

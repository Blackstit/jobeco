# JobEco

AI-powered job vacancy aggregator that collects postings from Telegram channels and web sources, structures them with LLMs, scores quality, enriches company data, and exposes everything through a modern admin panel and public API.

## What it does

**Ingest** — A Telethon userbot monitors Telegram channels for new posts. Web parsers (e.g. degencryptojobs.com) fetch listings on a schedule. Both feed into the same processing pipeline.

**AI Pipeline** — Each vacancy goes through:
- Pre-validation (cheap classifier rejects ads/memes/non-vacancies)
- Structured extraction (title, salary, stack, contacts, location, seniority, responsibilities, requirements)
- Multi-domain tagging and risk labeling
- 5-criterion weighted quality scoring (tasks clarity, compensation, tech stack, requirements logic, company profile)
- Company enrichment via Perplexity AI (website, industry, size, HQ, socials, logo)
- Embedding generation for deduplication and semantic search

**Admin Panel** — Dark-themed dashboard with vacancy browser (split-view with filters, sorting, AI scoring breakdown), source management (Telegram channels + web sources), analytics with charts, API key management, and parser logs.

**Public API** — Two endpoints with API key auth, rate limiting, full filtering, sorting, and semantic vector search.

## Tech stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11, FastAPI, SQLAlchemy (async) |
| Database | PostgreSQL 16 + pgvector |
| Telegram | Telethon (userbot), Aiogram v3 (admin bot) |
| AI/LLM | OpenRouter (GPT-4o, GPT-4o-mini), Perplexity AI |
| Embeddings | text-embedding-3-small (1536d) |
| Frontend | Jinja2 templates, Tailwind CSS, Chart.js |
| Infrastructure | Docker Compose |

## Quick start

```bash
cp env.example .env
# Edit .env: set ADMIN_BOT_TOKEN, TELETHON_API_ID/HASH, OPENROUTER_API_KEY

docker compose up -d --build
docker compose exec admin-bot alembic upgrade head
```

Admin panel: [http://localhost:8000](http://localhost:8000)

Default password: first 8 characters of your `OPENROUTER_API_KEY`.

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐
│  Telegram    │────▶│   Userbot    │────▶│                 │
│  Channels    │     │  (Telethon)  │     │    Processing   │
└─────────────┘     └──────────────┘     │    Pipeline     │
                                         │                 │
┌─────────────┐     ┌──────────────┐     │  • Classify     │
│  Web Sources │────▶│   Parsers    │────▶│  • Extract      │
│  (scheduled) │     │  (httpx)     │     │  • Score        │     ┌──────────┐
└─────────────┘     └──────────────┘     │  • Enrich       │────▶│ Postgres │
                                         │  • Embed        │     │ +pgvector│
                                         │  • Deduplicate  │     └────┬─────┘
                                         └─────────────────┘          │
                                                                      │
                    ┌──────────────┐     ┌─────────────────┐          │
                    │  Admin Panel │◀────│   FastAPI        │◀─────────┘
                    │  (browser)   │     │   Web Server     │
                    └──────────────┘     │                  │
                                         │  /api/public/*   │◀── API consumers
                    ┌──────────────┐     │                  │
                    │  Admin Bot   │◀────│  Aiogram v3      │
                    │  (Telegram)  │     └─────────────────┘
                    └──────────────┘
```

## Public API

Two endpoints, both require `X-API-Key` header:

### `GET /api/public/vacancies`

Paginated list with filters and sorting.

```bash
curl -H "X-API-Key: YOUR_TOKEN" \
  "https://host/api/public/vacancies?domains=web3&seniority=senior&score_min=5&sort=score_desc&per_page=10"
```

### `GET /api/public/vacancies/semantic-search`

Vector similarity search — same filters, plus a required `q` parameter.

```bash
curl -H "X-API-Key: YOUR_TOKEN" \
  "https://host/api/public/vacancies/semantic-search?q=rust+defi+backend&per_page=10"
```

**Available filters:** `domains`, `location_type`, `seniority`, `employment_type`, `salary_min_usd`, `salary_max_usd`, `score_min`, `score_max`, `risk_label`, `company_name`, `search`

**Sort options:** `date_desc` (default), `date_asc`, `salary_desc`, `salary_asc`, `score_desc`, `score_asc`

Each vacancy includes: structured fields, AI scoring breakdown, enriched company profile, typed contacts, and full text blocks.

Full documentation: [API.md](API.md) or `/api/docs` in the admin panel.

## Project structure

```
apps/
  admin_bot.py       Aiogram admin bot
  userbot.py         Telethon channel monitor
  web_admin.py       FastAPI admin panel + public API
jobeco/
  db/                SQLAlchemy models, session, base
  openrouter/        LLM client, company enrichment
  parsers/           Web source parsers (degencryptojobs, ...)
  processing/        Vacancy processing pipeline
  tg/                Telethon session management
  settings.py        Pydantic settings from env
templates/           Jinja2 HTML templates (Tailwind CSS)
alembic/             Database migrations
docker/              Dockerfile
```

## Environment variables

Copy `env.example` to `.env` and configure:

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | PostgreSQL connection string (asyncpg) |
| `ADMIN_BOT_TOKEN` | Telegram bot token for admin bot |
| `ADMIN_IDS` | Comma-separated Telegram user IDs for admin access |
| `TELETHON_API_ID` / `TELETHON_API_HASH` | Telegram API credentials for userbot |
| `OPENROUTER_API_KEY` | OpenRouter API key for LLM calls |
| `OPENROUTER_MODEL_ANALYZER` | Model for extraction (default: `gpt-4o`) |
| `OPENROUTER_MODEL_CLASSIFIER` | Model for pre-validation (default: `gpt-4o-mini`) |
| `EMBEDDING_MODEL` | Embedding model (default: `text-embedding-3-small`) |
| `DEDUP_THRESHOLD` | Cosine similarity threshold for dedup (default: `0.95`) |
| `SESSION_SECRET_KEY` | Secret for admin panel session cookies |

## License

Private repository. All rights reserved.

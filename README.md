# JobEco

AI-powered job market aggregator. Collects vacancies from Telegram channels and web sources, enriches them with LLMs, scores quality, builds company profiles, and serves everything through a modern admin panel and public API.

## Features

- **Multi-source ingestion** — Telegram channel monitoring (Telethon userbot) and scheduled web parsers (web3.career, cryptojobs.com, and more)
- **AI processing pipeline** — classification, structured extraction, role/seniority normalization, multi-domain tagging, risk labeling, 5-criterion quality scoring, company enrichment, embedding-based deduplication
- **Admin panel** — dashboard with live metrics, vacancy browser with filters and AI scoring breakdown, company directory, interactive market map graph, analytics, source management, API keys, logs
- **Market map** — force-directed graph visualization of the entire job market with grouping by domain/source/company/role, zoom-dependent rendering, and configurable display settings
- **Company directory** — searchable company cards with enriched profiles, filterable by industry, linked to their vacancies
- **Public API** — paginated listing and semantic vector search with full filtering, sorting, and API key auth

## Tech stack

| Layer | Stack |
|-------|-------|
| Backend | Python 3.11, FastAPI, SQLAlchemy (async) |
| Database | PostgreSQL 16 + pgvector |
| Telegram | Telethon, Aiogram v3 |
| AI | OpenRouter (GPT-4o / GPT-4o-mini), Perplexity AI, text-embedding-3-small |
| Frontend | Jinja2, Tailwind CSS, Chart.js, Canvas API |
| Infra | Docker Compose |

## Quick start

```bash
cp env.example .env
# Fill in: ADMIN_BOT_TOKEN, TELETHON_API_ID/HASH, OPENROUTER_API_KEY

docker compose up -d --build
docker compose exec admin-bot alembic upgrade head
```

Admin panel: `http://localhost:8000`

## Architecture

```
Telegram Channels ──▶ Userbot (Telethon) ──┐
                                           ▼
Web Sources ──▶ Parsers (httpx, scheduled) ──▶ Processing Pipeline ──▶ PostgreSQL + pgvector
                                                 │  classify            │
                                                 │  extract             │
                                                 │  normalize           │
                                                 │  score               │
                                                 │  enrich              │
                                                 │  embed / dedup       │
                                                 ▼                      ▼
                                           FastAPI Web Server      Admin Bot (Aiogram)
                                             │  Admin Panel
                                             │  Public API
                                             │  /api/public/*
```

## Public API

Two endpoints behind `X-API-Key` auth:

| Endpoint | Description |
|----------|-------------|
| `GET /api/public/vacancies` | Paginated list with filters and sorting |
| `GET /api/public/vacancies/semantic-search` | Vector similarity search (requires `q` param) |

Filters: `domains`, `location_type`, `seniority`, `role`, `employment_type`, `salary_min_usd`, `salary_max_usd`, `score_min`, `score_max`, `risk_label`, `company_name`, `search`

Sort: `date_desc`, `date_asc`, `salary_desc`, `salary_asc`, `score_desc`, `score_asc`

Full docs at `/api/docs` in the admin panel.

## Project structure

```
apps/
  admin_bot.py         Aiogram admin bot
  userbot.py           Telethon channel monitor
  web_admin.py         FastAPI admin panel + public API
jobeco/
  db/                  SQLAlchemy models, session
  openrouter/          LLM client, company enrichment
  parsers/             Web source parsers
  processing/          Vacancy pipeline, normalization
  tg/                  Telethon session management
  settings.py          Pydantic settings
templates/             Jinja2 templates (Tailwind CSS)
alembic/               Database migrations
docker/                Dockerfile
```

## License

Private repository. All rights reserved.

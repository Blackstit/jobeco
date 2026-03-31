# Job-Eco Public API

Base URL: `https://YOUR_HOST`

## Authentication

All endpoints require an API key passed via the `X-API-Key` header.

```
X-API-Key: <token>
```

Create and manage keys at `/api/keys` in the admin panel. The secret token is shown **once** at creation — save it immediately.

### Rate limits

Configured per key:

| Limit | Header on 429 |
|-------|---------------|
| `requests_per_minute` | Requests in a 1-minute sliding window |
| `daily_quota` | Requests in a 24-hour sliding window |

When exceeded the API returns `HTTP 429`.

---

## Endpoints

### `GET /api/public/vacancies`

Paginated list of vacancies with full data, filters, and sorting.

#### Query parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `page` | int | 1 | Page number (1-based) |
| `per_page` | int | 50 | Items per page (max 200) |
| `search` | string | — | Full-text search across title, company name, and raw text |
| `domains` | string[] | — | Filter by domain tags, repeatable (`domains=web3&domains=ai`). OR logic. |
| `location_type` | string | — | `remote`, `hybrid`, or `office` |
| `seniority` | string | — | `trainee`, `junior`, `middle`, `senior`, `lead`, `head`, `c-level` |
| `employment_type` | string | — | `full-time`, `part-time`, `project`, `freelance`, `internship` |
| `salary_min_usd` | int | — | Minimum salary (USD). Returns vacancies where `salary_max_usd >= value`. |
| `salary_max_usd` | int | — | Maximum salary (USD). Returns vacancies where `salary_min_usd <= value`. |
| `score_min` | int | — | Minimum AI quality score (0–10) |
| `score_max` | int | — | Maximum AI quality score (0–10) |
| `risk_label` | string | — | `high-risk` or `not-high-risk` (excludes high-risk) |
| `company_name` | string | — | Substring search by company name |
| `sort` | string | `date_desc` | Sort order (see below) |

#### Sort values

| Value | Description |
|-------|-------------|
| `date_desc` | Newest first (default) |
| `date_asc` | Oldest first |
| `salary_desc` | Highest salary first |
| `salary_asc` | Lowest salary first |
| `score_desc` | Highest AI score first |
| `score_asc` | Lowest AI score first |

#### Example

```bash
curl -s \
  -H "Accept: application/json" \
  -H "X-API-Key: YOUR_TOKEN" \
  "https://YOUR_HOST/api/public/vacancies?page=1&per_page=10&domains=web3&seniority=senior&score_min=5&sort=score_desc"
```

---

### `GET /api/public/vacancies/semantic-search`

Vector similarity search using embeddings. Accepts all the same filters as the list endpoint, plus a required query parameter.

#### Additional parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `q` | string | **(required)** | Natural-language search query. Text is embedded and matched against vacancy embeddings. |
| `sort` | string | `relevance` | Default sorts by embedding similarity. Accepts all sort values from the list endpoint. |

All other parameters from `GET /api/public/vacancies` are supported.

#### Example

```bash
curl -s \
  -H "Accept: application/json" \
  -H "X-API-Key: YOUR_TOKEN" \
  "https://YOUR_HOST/api/public/vacancies/semantic-search?q=rust%20backend%20defi&per_page=10&domains=web3"
```

---

## Response format

Both endpoints return the same structure:

```json
{
  "page": 1,
  "per_page": 10,
  "total": 142,
  "items": [...]
}
```

Semantic search also includes `"q": "your query"` at the top level.

### Item fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | int | Unique vacancy ID |
| `title` | string | Job title |
| `company_name` | string\|null | Company name |
| `role` | string\|null | Normalized role (e.g. "Developer", "Designer") |
| `domains` | string[] | Domain tags: `web3`, `ai`, `fintech`, `igaming`, `dev`, etc. |
| `risk_label` | string\|null | `null` (safe) or `"high-risk"` |
| `ai_score_value` | int\|null | AI quality score (0–10), integer |
| `location_type` | string\|null | `remote`, `hybrid`, or `office` |
| `salary_min_usd` | int\|null | Minimum salary in USD |
| `salary_max_usd` | int\|null | Maximum salary in USD |
| `currency` | string\|null | Original currency if not USD (e.g. `EUR`, `RUB`) |
| `seniority` | string\|null | `trainee`, `junior`, `middle`, `senior`, `lead`, `head`, `c-level` |
| `english_level` | string\|null | Required English level (e.g. `B2`, `C1`) |
| `employment_type` | string\|null | `full-time`, `part-time`, `project`, `freelance`, `internship` |
| `language_requirements` | object\|null | Language requirements, e.g. `{"english": "B2", "russian": "C1"}` |
| `experience_years` | int\|null | Required years of experience |
| `country_city` | string\|null | Location (e.g. `"San Francisco"`, `"Berlin"`) |
| `recruiter` | string\|null | Recruiter name or handle |
| `summary` | string\|null | AI-generated summary (language depends on key config) |
| `skills` | string[] | Tech stack / skill tags |
| `stack` | string[] | Alias for `skills` |
| `contacts` | object | Typed contacts, e.g. `{"Telegram": "@user", "Email": "a@b.com", "Application Form": "https://..."}` |
| `source_url` | string\|null | Link to the original posting (Telegram message or web page) |
| `source_channel` | string\|null | Source identifier (e.g. `"web:degencryptojobs"` or Telegram channel) |
| `created_at` | string | ISO 8601 timestamp |
| `description` | string\|null | Company/team description block |
| `responsibilities` | string\|null | Job responsibilities |
| `requirements` | string\|null | Job requirements |
| `conditions` | string\|null | Conditions and benefits |
| `raw_text` | string\|null | Original unprocessed vacancy text |
| `scoring` | object\|null | AI quality assessment (see below) |
| `company` | object\|null | Enriched company profile (see below) |
| `semantic_similarity` | float\|null | 0.0–1.0, only in semantic search results |

### `scoring` object

```json
{
  "total_score": 7.7,
  "overall_summary": "Well-defined vacancy with clear compensation...",
  "red_flags": ["No company website mentioned"],
  "scoring_results": [
    {
      "criterion": "Tasks & KPI clarity",
      "key": "tasks_and_kpi",
      "score": 8,
      "weight": 0.3,
      "summary": "Clear responsibilities with measurable outcomes..."
    },
    {
      "criterion": "Compensation clarity",
      "key": "compensation_clarity",
      "score": 9,
      "weight": 0.25,
      "summary": "Salary range explicitly stated in USD..."
    },
    {
      "criterion": "Stack & processes",
      "key": "tech_stack_and_ops",
      "score": 6,
      "weight": 0.2,
      "summary": "Tech stack listed but no info on processes..."
    },
    {
      "criterion": "Requirement logic",
      "key": "requirement_logic",
      "score": 7,
      "weight": 0.15,
      "summary": "Requirements are reasonable for the role..."
    },
    {
      "criterion": "Company profile",
      "key": "company_profile",
      "score": 8,
      "weight": 0.1,
      "summary": "Known company with public presence..."
    }
  ]
}
```

### `company` object

Enriched via Perplexity AI. `null` if company could not be identified.

```json
{
  "name": "Maple Finance",
  "website": "https://maple.finance",
  "logo_url": "https://...",
  "industry": "DeFi",
  "size": "50-200",
  "founded": "2019",
  "headquarters": "Melbourne, Australia",
  "summary": "Onchain asset manager providing institutional-grade lending...",
  "socials": {"twitter": "https://twitter.com/...", "linkedin": "https://linkedin.com/..."},
  "domains": ["web3", "fintech"]
}
```

---

## Key-level filters

Filters can be pre-configured at the API key level (set in the admin panel). Query parameters override key-level config when provided.

| Filter | Scope |
|--------|-------|
| `domains` | OR match against vacancy domains |
| `location_type` | Exact match |
| `risk_label` | Exact match |
| `salary_min_usd`, `salary_max_usd` | Range filter |
| `role` | Substring match (ILIKE) |
| `recruiter` | Substring match (ILIKE) |

Output language can also be set at the key level (`config.output.language`: `"en"` or `"ru"`). Default is English.

---

## Error responses

| Status | Meaning |
|--------|---------|
| 401 | Missing or invalid API key |
| 429 | Rate limit or daily quota exceeded |
| 503 | Embedding service unavailable (semantic search only) |

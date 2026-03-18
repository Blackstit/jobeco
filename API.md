# Job-Eco API

## 1) Раздел API в админке

Путь: `GET /api/keys` (требуется авторизация админки).

На странице можно:
- создать API-ключ (секретный токен показывается **один раз**)
- включать/выключать ключ
- обновлять фильтры и лимиты
- `Regenerate` — сгенерировать новый токен (старый перестанет работать)
- `Delete` — удалить ключ
- смотреть usage за последние 24 часа

Ключ указывается в запросах заголовком:
- `X-API-Key: <token>`

## 2) Публичный эндпоинт выдачи вакансий

Эндпоинт:
- `GET /api/public/vacancies`

Требование:
- ключ обязателен: `X-API-Key`

Query параметры (пагинация/включение доп.данных):
- `page` (int, default `1`)
- `per_page` (int, default `50`, max `200`)
- `include_blocks` (bool, default `0/false`) — добавить блоки `description/responsibilities/requirements/conditions`
- `include_raw_text` (bool, default `0/false`) — добавить поле `raw_text`

Пример запроса:
```bash
curl -s \
  -H "Accept: application/json" \
  -H "X-API-Key: YOUR_TOKEN" \
  "http://YOUR_HOST/api/public/vacancies?page=1&per_page=20"
```

Пример с блоками:
```bash
curl -s \
  -H "Accept: application/json" \
  -H "X-API-Key: YOUR_TOKEN" \
  "http://YOUR_HOST/api/public/vacancies?per_page=10&include_blocks=1"
```

## 3) Фильтры (задаются на уровне ключа)

Фильтры настраиваются в `GET /api/keys` → Create/Update.

Поддерживаемые фильтры:
- `domains` (string[], через запятую в UI)
  - работает как OR по доменам (если хотя бы одно доменное значение совпало)
  - домены приводятся к lowercase
- `role` (substring, `ILIKE`)
- `recruiter` (substring, `ILIKE`)
- `company_domain` (string, точное совпадение `Vacancy.company_domain`)
- `location_type` (`remote|hybrid|office`, точное совпадение)
- `risk_label` (`high-risk` — точное совпадение `Vacancy.risk_label`)
- `salary_min_usd` (int|null)
  - отбирает вакансии, где `salary_max_usd >= salary_min_usd`
- `salary_max_usd` (int|null)
  - отбирает вакансии, где `salary_min_usd <= salary_max_usd`

> Примечание: логика зарплат учитывает только записи, где нужные поля не `NULL`.

## 4) Формат ответа

Успешный ответ возвращает JSON:
```json
{
  "page": 1,
  "per_page": 20,
  "total": 123,
  "items": [
    {
      "id": 559,
      "title": "Web3 Rust Developer",
      "company_name": null,
      "role": "Developer",
      "domains": ["web3"],
      "risk_label": null,
      "ai_score_value": 8,
      "location_type": "remote",
      "salary_min_usd": 7000,
      "salary_max_usd": 12000,
      "recruiter": null,
      "summary": "…",
      "contacts": ["@SomeBot", "email@example.com", "..."],
      "source_url": null,
      "created_at": "2026-03-18T16:08:08.600111+00:00",
      "description": "…",            // только если include_blocks=1
      "responsibilities": "…",      // только если include_blocks=1
      "requirements": "…",          // только если include_blocks=1
      "conditions": "…"             // только если include_blocks=1
      "raw_text": "…"              // только если include_raw_text=1
    }
  ]
}
```

Пояснения:
- `summary` — берётся из `summary_en` (на проде сейчас всегда EN по умолчанию)
- `contacts` — список строк
- `source_url` — ссылка на оригинальный источник (если она извлечена)

## 5) Rate limits и usage

Лимиты задаются на уровне ключа в UI:
- `requests_per_minute` (RPM)
- `daily_quota` (суточная квота)

Если лимит превышен:
- вернётся `HTTP 429`
- в usage будет записан статус `429`

## 6) Админские эндпоинты (служебные)

Используются страницами UI (HTML формы), поэтому их обычно трогать не нужно:
- `GET /api/keys`
- `POST /api/keys/create`
- `POST /api/keys/{api_key_id}/toggle`
- `POST /api/keys/{api_key_id}/update`
- `POST /api/keys/{api_key_id}/regenerate`
- `POST /api/keys/{api_key_id}/delete`


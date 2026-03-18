# JobEco — AI-хаб вакансий из Telegram

JobEco собирает вакансии из Telegram-каналов и превращает “сырые посты” в структурированные записи, которые удобно фильтровать и использовать для витрин, ботов и публикаций.

Проект построен вокруг связки:
- **Telethon userbot** — читает новые сообщения в целевых каналах
- **Pipeline** — нормализует текст, считает embeddings, дедуплицирует, запускает OpenRouter для классификации и извлечения
- **PostgreSQL + pgvector** — хранит вакансии/каналы и векторные представления для дедупа
- **Admin Web (FastAPI + Jinja2 + Tailwind)** — панель администратора с поиском/фильтрами/категоризацией и действиями (reanalyze, fetch last 5, delete и т.д.)
- **OpenRouter** — LLM для анализа вакансий и каналов (domains/tags/risk + structured blocks + контакты)

> Если на OpenRouter заканчиваются кредиты/лимиты, задачи `Re-analyze` и “fetch last 5” могут падать — это штатная ситуация. UI показывает реальную причину (например `402 Payment Required`).

---

## Что уже умеет

### Ингест из Telegram
- Добавляйте каналы в админке
- Userbot отслеживает новые посты и передаёт текст в pipeline

### Предвалидация (дешёвый LLM-классификатор)
- До “дорогого” анализа проверяем: это реально вакансия или реклама/инфопост/мем
- Снижается количество мусора в базе

### AI-структурирование вакансий
LLM возвращает JSON со структурой, которую мы сохраняем в БД:
- `domains` (несколько доменов/ниш)
- `risk_label` (например `high-risk` как отдельный бейдж/фильтр)
- `summary_en` для карточки
- `description`, `responsibilities`, `requirements`, `conditions` (Markdown со списками)
- `contacts` (только прямые контакты HR/рекрутера; фильтрация хвостов канала)
- `standardized_title` (нормализованная должность)

### Дедупликация
- embeddings + cosine similarity
- порог задаётся в настройках (`DEDUP_THRESHOLD`)

### Семантический поиск (vector)
- public API поддерживает vector/semantic поиск по embeddings вакансий
- endpoint: `GET /api/public/vacancies/semantic-search`
- параметры: `q` (обязателен), `page`, `per_page`
- сортировка: по близости (cosine distance), ближайшие первыми
- фильтры (domains/role/recruiter/...) применяются так же, как и для `GET /api/public/vacancies` — на уровне API-ключа

### Admin Web
- Вакансии: split-view (список + правая панель деталей)
- Каналы: поиск/фильтры/сортировка + bulk действия (массовые обновления AI/“fetch last 5”)
- Красивый рендер списков из `Description / Responsibilities / Requirements`
- Удобное отображение контактов и “Original text” под спойлером

---

## Стек

- **Python / FastAPI** — админ web и API
- **SQLAlchemy (async)** — работа с БД
- **PostgreSQL + pgvector** — хранение вакансий/каналов и векторов
- **Telethon** — Telegram ингест
- **Aiogram (v3)** — Telegram admin-bot
- **OpenRouter** — LLM для классификации и извлечения данных
- **httpx / pydantic-settings / alembic** — инфраструктура интеграций и миграций
- **Tailwind CSS (через Jinja2 templates)** — UI админ-панели

---

## Быстрый старт (Docker Compose)

### 1) Подготовь `.env`
Файл секретный — **не коммить**.

```bash
cd /root/job-eco
cp env.example .env
# отредактируй: ADMIN_BOT_TOKEN, ADMIN_IDS, TELETHON_API_ID, TELETHON_API_HASH, OPENROUTER_API_KEY
```

### 2) Запуск
```bash
docker compose up -d --build
```

### 3) Миграции (1 раз)
```bash
docker compose exec admin-bot alembic upgrade head
```

### 4) Проверка
```bash
docker compose ps
docker compose logs -f admin-web
```

Admin Web доступен на `http://localhost:8000`.

---

## Логин в Admin Web

Текущий пароль формируется так:
- если задан `OPENROUTER_API_KEY` — пароль = первые **8** символов ключа
- иначе — fallback `admin123`

После входа можно управлять каналами и вакансией через UI.

---

## Конфигурация окружения

Смотри `env.example` в корне проекта.

Ключевые параметры:
- **DB**: `DATABASE_URL` / `POSTGRES_*`
- **Telegram**: `ADMIN_BOT_TOKEN`, `ADMIN_IDS`, `TELETHON_API_ID`, `TELETHON_API_HASH`
- **Telethon session path**: `TELETHON_SESSION_PATH` (файл хранится в `./data/telethon/`)
- **OpenRouter**:
  - `OPENROUTER_API_KEY`
  - `OPENROUTER_BASE_URL` (по умолчанию `https://openrouter.ai/api/v1`)
  - `OPENROUTER_MODEL_CLASSIFIER` (по умолчанию `gpt-4o-mini`)
  - `OPENROUTER_MODEL_ANALYZER` (по умолчанию `gpt-4o`)
- **Embeddings / Dedup**:
  - `EMBEDDING_MODEL`, `EMBEDDING_DIM`
  - `DEDUP_THRESHOLD`

---

## Telethon sessions и секреты

Telethon session хранится в `./data/telethon/`.

Чтобы избежать проблем:
- не пушь `.session` файлы в Git (они приватные)
- убедись, что session-файл не используется параллельно несколькими сервисами

---

## AI-формат полей (важно для UI)

Для полей:
- `description`
- `responsibilities`
- `requirements`
- `conditions`

LLM запрашивается возвращать **Markdown со списками** вида `- item`.

Admin Web рендерит это в буллет-листы, поэтому блоки выглядят аккуратно.

---

## Частые проблемы

### `Re-analyze failed: 402 Payment Required`
OpenRouter не может принять запрос из-за кредитов/лимитов (иногда ещё из-за слишком большого лимита max_tokens).

Что сделать:
- пополнить кредиты в OpenRouter
- при необходимости уменьшить лимиты запроса (при желании можно настроить в коде)

### `database is locked` у Telethon
Проверь:
- уникальность `TELETHON_SESSION_PATH` для каждого сервиса
- отсутствие параллельного использования одной и той же `.session`

---

## API (коротко)

Основные действия доступны из UI и через JSON API:
- `POST /api/vacancies/{vacancy_id}/reanalyze`
- `GET /api/vacancies/{vacancy_id}`
- `POST /api/vacancies/{vacancy_id}/delete`

Эндпоинты для каналов и bulk-действия тоже есть в `apps/web_admin.py`.

Публичная выдача вакансий по API-ключу:
- `GET /api/public/vacancies`
  - авторизация: заголовок `X-API-Key: <token>`
  - фильтры задаются на уровне ключа в UI (`/api/keys`)
  - пагинация: `page`, `per_page`
  - опционально: `include_blocks=1`, `include_raw_text=1`
- `GET /api/public/vacancies/semantic-search`
  - авторизация: заголовок `X-API-Key: <token>`
  - параметры: `q` (текст запроса для embedding), `page`, `per_page`
  - сортировка: по близости embedding (cosine distance)

Подробнее: `API.md`.

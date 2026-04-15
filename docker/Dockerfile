FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY . /app

RUN pip install --no-cache-dir -U pip && \
    pip install --no-cache-dir \
      "aiogram>=3.4,<4.0" \
      "telethon>=1.34,<2.0" \
      "pydantic>=2.6,<3.0" \
      "pydantic-settings>=2.2,<3.0" \
      "SQLAlchemy[asyncio]>=2.0,<3.0" \
      "asyncpg>=0.29,<1.0" \
      "psycopg2-binary>=2.9,<3.0" \
      "alembic>=1.13,<2.0" \
      "pgvector>=0.3,<1.0" \
      "httpx>=0.27,<1.0" \
      "tenacity>=8.2,<10.0" \
      "python-dotenv>=1.0,<2.0" \
      "structlog>=24.1,<25.0" \
      "fastapi>=0.115,<1.0" \
      "uvicorn[standard]>=0.30,<1.0" \
      "jinja2>=3.1,<4.0" \
      "python-multipart>=0.0.9,<0.1" \
      "itsdangerous>=2.1,<3.0" \
      "beautifulsoup4>=4.12,<5.0"


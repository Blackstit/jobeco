from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from pgvector.sqlalchemy import Vector

from jobeco.db.base import Base


class AdminUser(Base):
  __tablename__ = "admin_users"

  id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
  email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
  password_hash: Mapped[str] = mapped_column(Text, nullable=False)
  is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
  created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Channel(Base):
  __tablename__ = "channels"

  id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
  tg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
  username: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
  title: Mapped[str | None] = mapped_column(String(255), nullable=True)
  bio: Mapped[str | None] = mapped_column(Text, nullable=True)
  members_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
  enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
  created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

  # AI metadata (exists in DB)
  ai_domains: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
  ai_tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
  ai_risk_label: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)


class Company(Base):
  __tablename__ = "companies"

  id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
  name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
  name_lower: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
  website: Mapped[str | None] = mapped_column(Text, nullable=True)
  linkedin: Mapped[str | None] = mapped_column(Text, nullable=True)
  logo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
  summary: Mapped[str | None] = mapped_column(Text, nullable=True)
  industry: Mapped[str | None] = mapped_column(String(128), nullable=True)
  size: Mapped[str | None] = mapped_column(String(32), nullable=True)
  founded: Mapped[str | None] = mapped_column(String(8), nullable=True)
  headquarters: Mapped[str | None] = mapped_column(String(128), nullable=True)
  domains: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
  socials: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
  created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
  updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Vacancy(Base):
  __tablename__ = "vacancies"

  id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

  # raw telegram linkage
  tg_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
  tg_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
  tg_channel_username: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
  source_url: Mapped[str | None] = mapped_column(Text, nullable=True)

  # extracted core fields (minimal to start)
  company_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
  title: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
  location_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # remote/hybrid/office
  salary_min_usd: Mapped[int | None] = mapped_column(Integer, nullable=True)
  salary_max_usd: Mapped[int | None] = mapped_column(Integer, nullable=True)
  stack: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
  category: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # white-tech/igaming/high-risk-scam
  ai_score_value: Mapped[int | None] = mapped_column(Integer, nullable=True)

  # AI outputs
  summary_ru: Mapped[str | None] = mapped_column(Text, nullable=True)
  summary_en: Mapped[str | None] = mapped_column(Text, nullable=True)
  raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
  # "metadata" is reserved in SQLAlchemy Declarative API, so we use a different attribute name
  # while keeping the column name as "metadata".
  metadata_json: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")

  embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)

  created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

  # Structured fields already present in DB (keep optional to avoid strictness)
  company_url: Mapped[str | None] = mapped_column(Text, nullable=True)
  company_domain: Mapped[str | None] = mapped_column(String(64), nullable=True)
  company_size: Mapped[str | None] = mapped_column(String(32), nullable=True)
  standardized_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
  currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
  country_city: Mapped[str | None] = mapped_column(String(255), nullable=True)
  experience_years: Mapped[int | None] = mapped_column(Integer, nullable=True)
  english_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
  ai_summary_ru: Mapped[str | None] = mapped_column(Text, nullable=True)
  ai_summary_en: Mapped[str | None] = mapped_column(Text, nullable=True)
  ai_red_flags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
  external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True, unique=True)
  source_channel: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

  role: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
  seniority: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
  domain: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
  language: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
  recruiter: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
  contacts: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")

  description: Mapped[str | None] = mapped_column(Text, nullable=True)
  responsibilities: Mapped[str | None] = mapped_column(Text, nullable=True)
  requirements: Mapped[str | None] = mapped_column(Text, nullable=True)
  conditions: Mapped[str | None] = mapped_column(Text, nullable=True)

  content_type: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
  validation_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
  validation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

  risk_label: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
  domains: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
  company_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True)


class WebSource(Base):
  __tablename__ = "web_sources"

  id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
  slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
  name: Mapped[str] = mapped_column(String(255), nullable=False)
  url: Mapped[str] = mapped_column(Text, nullable=False)
  parser_type: Mapped[str] = mapped_column(String(64), nullable=False)
  enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
  sync_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="180")
  max_pages: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
  last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
  vacancies_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
  config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
  created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SystemSettings(Base):
  __tablename__ = "system_settings"

  id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
  # Single JSON blob for runtime config (parser/openrouter/prompts/admin UI).
  data: Mapped[dict] = mapped_column("data", JSONB, nullable=False, server_default="{}")
  updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ApiKey(Base):
  __tablename__ = "api_keys"

  id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
  name: Mapped[str] = mapped_column(String(255), nullable=False)

  # We store only the hash of the secret token.
  api_key_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)
  is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

  # Owner admin user (nullable for backward compatibility).
  owner_id: Mapped[int | None] = mapped_column(
    Integer, ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True, index=True
  )

  # Expiration date/time for the token. `NULL` means "infinite".
  expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

  # Arbitrary filter config and output preferences for this key.
  # Example:
  # {
  #   "filters": {"domains": ["web3"], "location_type": "remote"},
  #   "output": {"language": "en", "include_contacts": true}
  # }
  config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
  # Example:
  # {"requests_per_minute": 60, "daily_quota": 5000}
  limits: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")

  created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
  updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ApiKeyUsage(Base):
  __tablename__ = "api_key_usage"

  id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
  api_key_id: Mapped[int] = mapped_column(Integer, ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False, index=True)
  endpoint: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
  status_code: Mapped[int] = mapped_column(Integer, nullable=False)
  requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class ParserLog(Base):
  __tablename__ = "parser_logs"

  id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
  created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

  # Example values: INFO / WARNING / ERROR
  level: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

  # Short event key, used for grouping/filters in future.
  event: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

  # Human readable message (English as requested by UI).
  message_en: Mapped[str] = mapped_column(Text, nullable=False)

  channel_username: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
  tg_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
  vacancy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

  extra: Mapped[dict] = mapped_column(JSONB(astext_type=Text()), nullable=False, server_default="{}")


class DocArticle(Base):
  __tablename__ = "doc_articles"

  id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
  section: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
  title: Mapped[str] = mapped_column(String(256), nullable=False)
  slug: Mapped[str] = mapped_column(String(256), nullable=False, unique=True, index=True)
  content: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
  sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
  is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
  created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
  updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

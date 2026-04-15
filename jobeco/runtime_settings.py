from __future__ import annotations

import time
from copy import deepcopy
from typing import Any

import structlog
from sqlalchemy import select

from jobeco.db.models import SystemSettings
from jobeco.db.session import SessionLocal
from jobeco.settings import settings as env_settings

log = structlog.get_logger()

_CACHE: dict[str, Any] | None = None
_CACHE_TS: float = 0.0


def _deep_get(dct: dict[str, Any] | None, path: list[str], default: Any = None) -> Any:
  cur: Any = dct or {}
  for key in path:
    if not isinstance(cur, dict) or key not in cur:
      return default
    cur = cur[key]
  return cur


def _hash_password_sha256(pw: str) -> str:
  # Minimal deterministic hash; for production consider bcrypt/argon2.
  import hashlib

  return hashlib.sha256(pw.encode("utf-8")).hexdigest()


async def load_system_settings_raw() -> dict[str, Any]:
  async with SessionLocal() as s:
    row = (await s.execute(select(SystemSettings).order_by(SystemSettings.id.asc()).limit(1))).scalar_one_or_none()
    if not row:
      return {}
    return row.data or {}


async def get_runtime_settings(ttl_seconds: int = 30) -> dict[str, Any]:
  """
  Runtime settings resolved from DB (system_settings.data) over env defaults.

  Kept cached because it's used inside per-message pipeline functions.
  """
  global _CACHE, _CACHE_TS
  now = time.time()
  if _CACHE is not None and now - _CACHE_TS < ttl_seconds:
    return _CACHE

  raw = await load_system_settings_raw()

  parser = raw.get("parser") or {}
  openrouter = raw.get("openrouter") or {}
  prompts = raw.get("prompts") or {}
  admin = raw.get("admin") or {}
  limits = raw.get("limits") or {}

  resolved = {
    "parser": {
      "dedup_threshold": float(parser.get("dedup_threshold", env_settings.dedup_threshold)),
      "enabled": bool(parser.get("enabled", True)),
    },
    "openrouter": {
      "api_key": str(openrouter.get("api_key") or env_settings.openrouter_api_key or ""),
      "base_url": str(openrouter.get("base_url") or env_settings.openrouter_base_url or "https://openrouter.ai/api/v1"),
      "model_classifier": str(openrouter.get("model_classifier") or env_settings.openrouter_model_classifier),
      "model_analyzer": str(openrouter.get("model_analyzer") or env_settings.openrouter_model_analyzer),
      "max_tokens_analyzer": int(openrouter.get("max_tokens_analyzer") or 4000),
    },
    "limits": {
      "prevalidate_max_chars": int(limits.get("prevalidate_max_chars") or 6000),
      "analyzer_max_chars": int(limits.get("analyzer_max_chars") or 12000),
      "channel_max_chars": int(limits.get("channel_max_chars") or 7000),
    },
    "prompts": {
      "vacancy_analyzer_system": prompts.get("vacancy_analyzer_system"),
      "vacancy_prevalidate_system": prompts.get("vacancy_prevalidate_system"),
      "channel_categorizer_system": prompts.get("channel_categorizer_system"),
    },
    "admin": {
      "admin_password_hash": admin.get("admin_password_hash"),
    },
  }

  _CACHE = resolved
  _CACHE_TS = now
  return resolved


async def upsert_system_settings(new_data: dict[str, Any]) -> None:
  async with SessionLocal() as s:
    row = (await s.execute(select(SystemSettings).order_by(SystemSettings.id.asc()).limit(1))).scalar_one_or_none()
    if row:
      row.data = new_data
    else:
      s.add(SystemSettings(data=new_data))
    await s.commit()

  # Bust cache
  global _CACHE_TS
  _CACHE_TS = 0.0


async def set_admin_password_hash(pw_plain: str) -> None:
  runtime = await load_system_settings_raw()
  admin = runtime.get("admin") or {}
  admin["admin_password_hash"] = _hash_password_sha256(pw_plain)
  runtime["admin"] = admin
  await upsert_system_settings(runtime)


__all__ = ["get_runtime_settings", "upsert_system_settings", "set_admin_password_hash", "load_system_settings_raw"]


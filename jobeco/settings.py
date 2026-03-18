from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
  model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

  # DB
  database_url: str = "postgresql+asyncpg://jobeco:jobeco@postgres:5432/jobeco"

  # Telegram
  admin_bot_token: str = ""
  admin_ids: str = ""
  telethon_api_id: int = 0
  telethon_api_hash: str = ""
  telethon_session_path: str = "/data/telethon/userbot.session"

  # OpenRouter / embeddings
  openrouter_api_key: str = ""
  openrouter_base_url: str = "https://openrouter.ai/api/v1"
  openrouter_model_classifier: str = "gpt-4o-mini"
  openrouter_model_analyzer: str = "gpt-4o"
  embedding_model: str = "text-embedding-3-small"
  embedding_dim: int = 1536
  dedup_threshold: float = 0.95

  @property
  def admin_id_set(self) -> set[int]:
    vals = []
    for part in (self.admin_ids or "").replace(" ", "").split(","):
      if not part:
        continue
      try:
        vals.append(int(part))
      except ValueError:
        continue
    return set(vals)


settings = Settings()

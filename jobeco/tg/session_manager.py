from __future__ import annotations

from dataclasses import dataclass

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from jobeco.settings import settings


@dataclass
class PendingLogin:
  client: TelegramClient
  phone: str
  needs_password: bool = False


async def begin_login(phone: str) -> PendingLogin:
  if not settings.telethon_api_id or not settings.telethon_api_hash:
    raise RuntimeError("Set TELETHON_API_ID and TELETHON_API_HASH in .env")

  client = TelegramClient(settings.telethon_session_path, settings.telethon_api_id, settings.telethon_api_hash)
  await client.connect()
  await client.send_code_request(phone)
  return PendingLogin(client=client, phone=phone)


async def submit_code(pending: PendingLogin, code: str) -> PendingLogin:
  try:
    await pending.client.sign_in(pending.phone, code)
    return pending
  except SessionPasswordNeededError:
    pending.needs_password = True
    return pending


async def submit_password(pending: PendingLogin, password: str) -> PendingLogin:
  await pending.client.sign_in(password=password)
  pending.needs_password = False
  return pending


async def finalize(pending: PendingLogin) -> None:
  # Session is saved automatically into TELETHON_SESSION_PATH
  await pending.client.disconnect()


from __future__ import annotations

import asyncio

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from jobeco.settings import settings


async def main() -> None:
  if not settings.telethon_api_id or not settings.telethon_api_hash:
    raise RuntimeError("Set TELETHON_API_ID and TELETHON_API_HASH in .env")

  print(f"Session file: {settings.telethon_session_path}")
  phone = input("Phone (in international format, e.g. +123456789): ").strip()
  if not phone:
    raise SystemExit("Phone is required")

  client = TelegramClient(settings.telethon_session_path, settings.telethon_api_id, settings.telethon_api_hash)
  await client.connect()

  if await client.is_user_authorized():
    print("Already authorized, nothing to do.")
    await client.disconnect()
    return

  await client.send_code_request(phone)
  code = input("Code from Telegram: ").strip()
  try:
    await client.sign_in(phone, code)
  except SessionPasswordNeededError:
    pwd = input("2FA password: ").strip()
    await client.sign_in(password=pwd)

  print("✅ Authorized. Session saved.")
  await client.disconnect()


if __name__ == "__main__":
  asyncio.run(main())


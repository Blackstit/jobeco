from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import select, update
from telethon import TelegramClient, events
from telethon.tl.functions.channels import JoinChannelRequest, GetFullChannelRequest

from jobeco.db.models import Channel
from jobeco.db.session import SessionLocal
from jobeco.logging import configure_logging
from jobeco.processing.pipeline import process_message
from jobeco.settings import settings

log = structlog.get_logger()


async def ensure_channels(client: TelegramClient) -> None:
  async with SessionLocal() as s:
    channels = (await s.execute(select(Channel).order_by(Channel.id.asc()))).scalars().all()

  for ch in channels:
    if not ch.username:
      continue
    try:
      entity = await client.get_entity(ch.username)
      # получить полную информацию о канале (bio, участники)
      full = await client(GetFullChannelRequest(entity))
      bio = getattr(full.full_chat, "about", None)
      members = getattr(full.full_chat, "participants_count", None)

      async with SessionLocal() as s:
        await s.execute(
          update(Channel)
          .where(Channel.id == ch.id)
          .values(
            tg_id=getattr(entity, "id", ch.tg_id),
            username=getattr(entity, "username", ch.username),
            title=getattr(entity, "title", ch.title),
            bio=bio,
            members_count=members,
          )
        )
        await s.commit()
      try:
        await client(JoinChannelRequest(entity))
      except Exception:
        # already joined / not allowed / private - ignore in MVP
        pass
      log.info("channel_ready", username=ch.username, tg_id=getattr(entity, "id", None))
    except Exception as e:
      log.warning("channel_resolve_failed", username=ch.username, error=str(e))


async def main_async() -> None:
  configure_logging()

  if not settings.telethon_api_id or not settings.telethon_api_hash:
    raise RuntimeError("TELETHON_API_ID/TELETHON_API_HASH must be set")

  client = TelegramClient(settings.telethon_session_path, settings.telethon_api_id, settings.telethon_api_hash)
  await client.connect()
  if not await client.is_user_authorized():
    raise RuntimeError(
      "Telethon session is not authorized. Run:\n"
      "  docker compose run --rm -it userbot python -m jobeco.tg.login_cli"
    )

  await ensure_channels(client)

  # Получаем список разрешённых каналов из БД
  async def get_allowed_channels() -> set[int | str]:
    """Возвращает set с tg_id и username разрешённых каналов."""
    async with SessionLocal() as s:
      channels = (await s.execute(select(Channel))).scalars().all()
    allowed = set()
    for ch in channels:
      if ch.tg_id:
        allowed.add(ch.tg_id)
      if ch.username:
        allowed.add(ch.username)
    return allowed

  allowed_channels = await get_allowed_channels()
  log.info("allowed_channels_loaded", count=len(allowed_channels))

  @client.on(events.NewMessage())
  async def handler(event: events.NewMessage.Event) -> None:
    try:
      # Проверяем, что сообщение из разрешённого канала
      chat = event.chat
      if not chat:
        return
      
      # Пропускаем личные чаты (проверяем, что это канал/группа)
      # В Telethon: личные чаты имеют id > 0, каналы/группы имеют id < 0
      chat_id = getattr(chat, "id", None)
      chat_username = getattr(chat, "username", None)
      
      # Проверяем, что это канал (broadcast), а не группа
      # В Telethon каналы имеют broadcast=True, а группы/чаты часто megagroup=True
      is_broadcast = getattr(chat, "broadcast", False)
      is_megagroup = getattr(chat, "megagroup", False)
      if not is_broadcast and not is_megagroup:
        return
      
      # Проверяем, что канал в списке разрешённых
      is_allowed = False
      if chat_id:
        # Telethon для каналов/групп часто использует id со знаком.
        # В БД мы храним tg_id как положительное число, поэтому сравниваем и модуль.
        if chat_id in allowed_channels or abs(chat_id) in allowed_channels:
          is_allowed = True
      if (not is_allowed) and chat_username and chat_username in allowed_channels:
        is_allowed = True
      
      if not is_allowed:
        log.debug("message_skipped_not_allowed", chat_id=chat_id, username=chat_username)
        return
      
      await process_message(event)
    except Exception as e:
      log.exception("process_message_failed", error=str(e))

  log.info("userbot_started")
  await client.run_until_disconnected()


def main() -> None:
  asyncio.run(main_async())


if __name__ == "__main__":
  main()
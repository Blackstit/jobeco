from __future__ import annotations

import asyncio
import re

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
  CallbackQuery,
  InlineKeyboardButton,
  InlineKeyboardMarkup,
  KeyboardButton,
  Message,
  ReplyKeyboardMarkup,
)
import sqlalchemy as sa
from sqlalchemy import delete, insert, select

from jobeco.db.models import Channel, Vacancy
from jobeco.db.session import SessionLocal, engine
from jobeco.logging import configure_logging
from jobeco.settings import settings
from jobeco.tg.session_manager import begin_login, finalize, submit_code, submit_password, PendingLogin


MAIN_KB = ReplyKeyboardMarkup(
  keyboard=[
    [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="📢 Каналы")],
    [KeyboardButton(text="🔑 Сессия")],
  ],
  resize_keyboard=True,
)

def _is_admin(user_id: int) -> bool:
  return not settings.admin_id_set or user_id in settings.admin_id_set


async def migrate() -> None:
  # Alembic inside container is simplest; for MVP do nothing here.
  # Migrations are applied via scripts/startup or manual command.
  return None


async def cmd_start(message: Message) -> None:
  if not message.from_user or not _is_admin(message.from_user.id):
    return
  await message.answer("Job-Eco Admin Bot", reply_markup=MAIN_KB)


async def show_stats(message: Message) -> None:
  if not message.from_user or not _is_admin(message.from_user.id):
    return
  async with SessionLocal() as s:
    total = (await s.execute(select(Vacancy.id))).scalars().all()
    total_count = len(total)
    channels_count = len((await s.execute(select(Channel.id))).scalars().all())
  await message.answer(f"📊 Всего вакансий: {total_count}\n📢 Каналов: {channels_count}")


CHANNEL_RE = re.compile(r"(?:https?://)?t\\.me/([^\\s/?#]+)", re.IGNORECASE)

_waiting_add_channel: set[int] = set()


async def channels_help(message: Message) -> None:
  if not message.from_user or not _is_admin(message.from_user.id):
    return
  async with SessionLocal() as s:
    rows = (await s.execute(select(Channel).order_by(Channel.id.desc()).limit(50))).scalars().all()

  text_lines = ["📢 Каналы"]
  if not rows:
    text_lines.append("\nСписок каналов пуст.")
  else:
    for c in rows:
      label = c.title or c.username or f"id {c.tg_id}"
      extra = []
      if c.members_count is not None:
        extra.append(f"{c.members_count} подписчиков")
      if c.username:
        extra.append(f"@{c.username}")
      if extra:
        text_lines.append(f"- {label} ({', '.join(extra)})")
      else:
        text_lines.append(f"- {label}")

  buttons: list[list[InlineKeyboardButton]] = []
  for c in rows:
    label = c.title or c.username or f"id {c.tg_id}"
    buttons.append(
      [InlineKeyboardButton(text=label, callback_data=f"ch_{c.id}")]
    )
  buttons.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="add_channel")])
  kb = InlineKeyboardMarkup(inline_keyboard=buttons)

  await message.answer("\n".join(text_lines), reply_markup=kb)


async def cb_add_channel(callback: CallbackQuery) -> None:
  if not callback.from_user or not _is_admin(callback.from_user.id):
    await callback.answer("Нет прав", show_alert=True)
    return

  _waiting_add_channel.add(callback.from_user.id)
  await callback.message.answer(
    "Отправь ссылку вида `t.me/username`, `@username` или просто пересылай пост из канала.",
    parse_mode="Markdown",
  )
  await callback.answer()


async def cb_show_channel(callback: CallbackQuery) -> None:
  if not callback.from_user or not _is_admin(callback.from_user.id):
    await callback.answer("Нет прав", show_alert=True)
    return
  try:
    _, id_str = callback.data.split("_", 1)
    ch_id = int(id_str)
  except Exception:
    await callback.answer()
    return

  async with SessionLocal() as s:
    ch = (await s.execute(select(Channel).where(Channel.id == ch_id))).scalar_one_or_none()
  if not ch:
    await callback.answer("Канал не найден", show_alert=True)
    return

  bio_preview = (ch.bio or "").strip()
  if len(bio_preview) > 300:
    bio_preview = bio_preview[:300] + "..."

  text = "📢 Канал\n"
  text += f"- ID: {ch.id}\n"
  text += f"- tg_id: {ch.tg_id}\n"
  if ch.username:
    text += f"- username: @{ch.username}\n"
  if ch.title:
    text += f"- title: {ch.title}\n"
  if ch.members_count is not None:
    text += f"- подписчиков: {ch.members_count}\n"
  if bio_preview:
    text += f"\n{bio_preview}"

  await callback.message.answer(text)
  await callback.answer()


async def list_channels(message: Message) -> None:
  if not message.from_user or not _is_admin(message.from_user.id):
    return
  async with SessionLocal() as s:
    rows = (await s.execute(select(Channel).order_by(Channel.id.desc()).limit(200))).scalars().all()
  if not rows:
    await message.answer("Список каналов пуст. Пришли ссылку вида `t.me/username`.", parse_mode="Markdown")
    return
  text = "📢 Каналы:\n" + "\n".join(
    f"- {c.username or '(no-username)'} (tg_id={c.tg_id})" for c in rows
  )
  await message.answer(text)


async def del_channel(message: Message) -> None:
  if not message.from_user or not _is_admin(message.from_user.id):
    return
  parts = (message.text or "").split(maxsplit=1)
  if len(parts) != 2:
    await message.answer("Использование: `/del username`", parse_mode="Markdown")
    return
  username = parts[1].lstrip("@").strip()
  async with SessionLocal() as s:
    await s.execute(delete(Channel).where(Channel.username == username))
    await s.commit()
  await message.answer(f"✅ Удалил канал: {username}")


async def add_channel_from_text(message: Message) -> None:
  if not message.from_user:
    return
  if not _is_admin(message.from_user.id):
    await message.answer(
      "У тебя нет прав использовать этот бот.\n"
      "Добавь свой числовой Telegram ID в переменную `ADMIN_IDS` в `.env` и перезапусти контейнер `admin-bot`.",
      parse_mode="Markdown",
    )
    return

  # добавляем канал только после нажатия инлайн-кнопки
  if message.from_user.id not in _waiting_add_channel:
    return

  tg_id: int | None = None
  username: str | None = None

  # 1) Пересланный пост из канала
  if message.forward_from_chat:
    chat = message.forward_from_chat
    tg_id = chat.id
    username = (chat.username or "").strip() or None
  else:
    raw = (message.text or "").strip()

    # 2) t.me/username
    m = CHANNEL_RE.search(raw)
    if m:
      username = m.group(1).strip().lstrip("@").rstrip("/").strip()
    else:
      # 3) @username или просто username — для MVP принимаем ЛЮБОЙ непустой текст как username
      candidate = raw
      # убираем t.me/, @ и пробелы
      candidate = candidate.replace("https://", "").replace("http://", "")
      candidate = candidate.replace("t.me/", "").lstrip("@").strip()
      # иногда Telegram добавляет невидимые символы – попробуем очистить
      candidate = re.sub(r"[^A-Za-z0-9_]", "", candidate)
      if not candidate:
        await message.answer(
          "Не понял канал.\nПришли, пожалуйста, ссылку вида `t.me/username`, `@username` или просто пересылай пост.",
          parse_mode="Markdown",
        )
        return
      username = candidate

  # NOTE: tg_id может быть неизвестен; userbot позже сам получит entity и обновит.
  async with SessionLocal() as s:
    q = select(Channel)
    if username:
      q = q.where(Channel.username == username)
    elif tg_id is not None:
      q = q.where(Channel.tg_id == tg_id)
    existing = (await s.execute(q)).scalar_one_or_none()
    if existing:
      await message.answer(
        f"Этот канал уже есть в списке: {existing.username or existing.tg_id}"
      )
      _waiting_add_channel.discard(message.from_user.id)
      return

    ch = Channel(tg_id=tg_id, username=username, title=None)
    s.add(ch)
    await s.commit()

  _waiting_add_channel.discard(message.from_user.id)

  await message.answer(
    f"✅ Добавил канал: {username or tg_id}\nUserBot сам вступит и начнёт слушать."
  )


async def show_session(message: Message) -> None:
  if not message.from_user or not _is_admin(message.from_user.id):
    return
  # Session manager entrypoint
  await message.answer(
    f"🔑 Telethon session path: `{settings.telethon_session_path}`\n"
    "Команды:\n"
    "- `/login +123456789` (телефон)\n"
    "- `/code 12345` (код из SMS/Telegram)\n"
    "- `/password your_2fa_password` (если включен 2FA)\n",
    parse_mode="Markdown",
  )


_pending: dict[int, PendingLogin] = {}


async def login_start(message: Message) -> None:
  if not message.from_user or not _is_admin(message.from_user.id):
    return
  parts = (message.text or "").split(maxsplit=1)
  if len(parts) != 2:
    await message.answer("Использование: `/login +123456789`", parse_mode="Markdown")
    return
  phone = parts[1].strip()
  pending = await begin_login(phone)
  _pending[message.from_user.id] = pending
  await message.answer("✅ Код отправлен. Теперь пришли: `/code 12345`", parse_mode="Markdown")


async def login_code(message: Message) -> None:
  if not message.from_user or not _is_admin(message.from_user.id):
    return
  pending = _pending.get(message.from_user.id)
  if not pending:
    await message.answer("Нет активной сессии. Начни с `/login +123...`", parse_mode="Markdown")
    return
  parts = (message.text or "").split(maxsplit=1)
  if len(parts) != 2:
    await message.answer("Использование: `/code 12345`", parse_mode="Markdown")
    return
  code = parts[1].strip()
  pending = await submit_code(pending, code)
  _pending[message.from_user.id] = pending
  if pending.needs_password:
    await message.answer("🔐 Нужен пароль 2FA. Пришли: `/password your_password`", parse_mode="Markdown")
    return
  await finalize(pending)
  _pending.pop(message.from_user.id, None)
  await message.answer("✅ Telethon авторизован, session сохранён. Теперь можно стартовать `userbot`.")


async def login_password(message: Message) -> None:
  if not message.from_user or not _is_admin(message.from_user.id):
    return
  pending = _pending.get(message.from_user.id)
  if not pending:
    await message.answer("Нет активной сессии. Начни с `/login +123...`", parse_mode="Markdown")
    return
  parts = (message.text or "").split(maxsplit=1)
  if len(parts) != 2:
    await message.answer("Использование: `/password your_password`", parse_mode="Markdown")
    return
  pwd = parts[1]
  pending = await submit_password(pending, pwd)
  await finalize(pending)
  _pending.pop(message.from_user.id, None)
  await message.answer("✅ Telethon авторизован, session сохранён. Теперь можно стартовать `userbot`.")


def main() -> None:
  configure_logging()
  if not settings.admin_bot_token or settings.admin_bot_token == "CHANGE_ME":
    raise RuntimeError("Set ADMIN_BOT_TOKEN in your .env (copy from env.example).")
  bot = Bot(settings.admin_bot_token)
  dp = Dispatcher()

  dp.message.register(cmd_start, CommandStart())
  dp.message.register(cmd_start, Command("start"))
  dp.message.register(list_channels, Command("list"))
  dp.message.register(del_channel, Command("del"))
  dp.message.register(login_start, Command("login"))
  dp.message.register(login_code, Command("code"))
  dp.message.register(login_password, Command("password"))

  dp.message.register(show_stats, F.text == "📊 Статистика")
  dp.message.register(channels_help, F.text == "📢 Каналы")
  dp.message.register(show_session, F.text == "🔑 Сессия")

  dp.callback_query.register(cb_add_channel, F.data == "add_channel")
  dp.callback_query.register(cb_show_channel, F.data.startswith("ch_"))

  dp.message.register(add_channel_from_text, F.text)

  asyncio.run(dp.start_polling(bot))


if __name__ == "__main__":
  main()
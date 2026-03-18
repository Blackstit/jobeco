from __future__ import annotations
from datetime import datetime

from fastapi import FastAPI, Request, Query, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
import httpx
from sqlalchemy import select, func, desc, or_, update, delete, text
from starlette.exceptions import HTTPException as StarletteHTTPException
from telethon import TelegramClient
from telethon.tl.functions.channels import GetFullChannelRequest

from jobeco.db.session import SessionLocal
from jobeco.db.models import Vacancy, Channel, SystemSettings, AdminUser
from jobeco.settings import settings
from jobeco.openrouter.client import categorize_channel
from jobeco.processing.pipeline import process_text_message
from jobeco.openrouter.client import analyze_with_openrouter
from jobeco.runtime_settings import (
  get_runtime_settings,
  upsert_system_settings,
  load_system_settings_raw,
)
from jobeco.auth.passwords import hash_password_pbkdf2, verify_password_pbkdf2


app = FastAPI(title="Job-Eco Admin")
# Stable secret key is required for sessions to survive restarts.
_session_secret = settings.session_secret_key or settings.openrouter_api_key or "jobeco_session_dev_secret"
app.add_middleware(SessionMiddleware, secret_key=_session_secret)

templates = Jinja2Templates(directory="templates")

async def require_auth(request: Request):
  """Проверка авторизации."""
  if not request.session.get("authenticated"):
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

  user_id = request.session.get("user_id")
  if not user_id:
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

  async with SessionLocal() as s:
    u = (await s.execute(select(AdminUser).where(AdminUser.id == int(user_id), AdminUser.is_active == True))).scalar_one_or_none()
    if not u:
      raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
  return True


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
  # Для HTML запросов на 401 делаем редирект на /login, чтобы не показывать JSON.
  if exc.status_code == 401:
    accept = request.headers.get("accept", "")
    if "text/html" in accept or "*/*" in accept or accept == "":
      return RedirectResponse(url="/login", status_code=303)
    return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
  return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
  if request.session.get("authenticated"):
    return RedirectResponse(url="/", status_code=303)
  return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
  email = (email or "").strip().lower()
  if not email or not password:
    return templates.TemplateResponse(
      "login.html",
      {"request": request, "error": "Введите email и пароль"},
      status_code=401,
    )

  async with SessionLocal() as s:
    u = (await s.execute(select(AdminUser).where(AdminUser.email == email))).scalar_one_or_none()
    if not u or not u.is_active:
      return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Неверный email или пароль"},
        status_code=401,
      )

    if not verify_password_pbkdf2(password, u.password_hash):
      return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Неверный email или пароль"},
        status_code=401,
      )

    request.session["authenticated"] = True
    request.session["user_id"] = u.id
    request.session["user_email"] = u.email
    return RedirectResponse(url="/", status_code=303)


@app.get("/logout")
async def logout(request: Request):
  request.session.clear()
  return RedirectResponse(url="/login", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(
  request: Request,
  _: bool = Depends(require_auth),
  tab: str = Query("parser"),
):
  runtime = await get_runtime_settings()
  prompts = runtime.get("prompts") or {}
  saved = request.query_params.get("saved") == "1"

  allowed_tabs = {"parser", "openrouter", "prompts", "sessions", "users"}
  if tab not in allowed_tabs:
    tab = "parser"

  users = None
  if tab == "users":
    async with SessionLocal() as s:
      users = (await s.execute(select(AdminUser).order_by(AdminUser.id.asc()))).scalars().all()

  return templates.TemplateResponse(
    "settings.html",
    {
      "request": request,
      "tab": tab,
      "runtime": runtime,
      "prompts": prompts,
      "env": settings,
      "saved": saved,
      "users": users,
    },
  )


@app.post("/settings", response_class=HTMLResponse)
async def settings_save(
  request: Request,
  _: bool = Depends(require_auth),
  tab: str = Query("parser"),
):
  allowed_tabs = {"parser", "openrouter", "prompts", "sessions", "users"}
  if tab not in allowed_tabs:
    tab = "parser"

  form = await request.form()
  raw = await load_system_settings_raw()

  if tab == "parser":
    dedup_threshold = form.get("dedup_threshold")
    prevalidate_max_chars = form.get("prevalidate_max_chars")
    analyzer_max_chars = form.get("analyzer_max_chars")
    channel_max_chars = form.get("channel_max_chars")
    if not all([dedup_threshold, prevalidate_max_chars, analyzer_max_chars, channel_max_chars]):
      raise HTTPException(status_code=400, detail="Missing parser settings fields")
    raw.setdefault("parser", {})["dedup_threshold"] = float(dedup_threshold)
    raw.setdefault("limits", {})["prevalidate_max_chars"] = int(prevalidate_max_chars)
    raw.setdefault("limits", {})["analyzer_max_chars"] = int(analyzer_max_chars)
    raw.setdefault("limits", {})["channel_max_chars"] = int(channel_max_chars)
    await upsert_system_settings(raw)

  elif tab == "openrouter":
    raw.setdefault("openrouter", {})
    api_key = (form.get("api_key") or "").strip()
    model_classifier = form.get("model_classifier")
    model_analyzer = form.get("model_analyzer")
    max_tokens_analyzer = form.get("max_tokens_analyzer")
    if not all([model_classifier, model_analyzer, max_tokens_analyzer]):
      raise HTTPException(status_code=400, detail="Missing openrouter settings fields")
    if api_key:
      raw["openrouter"]["api_key"] = api_key
    raw["openrouter"]["model_classifier"] = str(model_classifier).strip()
    raw["openrouter"]["model_analyzer"] = str(model_analyzer).strip()
    raw["openrouter"]["max_tokens_analyzer"] = int(max_tokens_analyzer)
    await upsert_system_settings(raw)

  elif tab == "prompts":
    raw.setdefault("prompts", {})
    a = (form.get("vacancy_analyzer_system") or "").strip()
    p = (form.get("vacancy_prevalidate_system") or "").strip()
    c = (form.get("channel_categorizer_system") or "").strip()
    if a:
      raw["prompts"]["vacancy_analyzer_system"] = a
    if p:
      raw["prompts"]["vacancy_prevalidate_system"] = p
    if c:
      raw["prompts"]["channel_categorizer_system"] = c
    await upsert_system_settings(raw)

  elif tab == "users":
    user_id_raw = form.get("user_id")
    email_raw = (form.get("email") or "").strip().lower()
    new_pw_raw = (form.get("password") or "").strip()
    is_active_raw = (form.get("is_active") or "").lower()
    is_active = is_active_raw in ("1", "true", "yes", "on")

    async with SessionLocal() as s:
      # Update existing user (by id if provided, otherwise by email if present).
      if user_id_raw:
        try:
          user_id = int(user_id_raw)
        except ValueError:
          raise HTTPException(status_code=400, detail="Invalid user_id")

        u = (await s.execute(select(AdminUser).where(AdminUser.id == user_id))).scalar_one_or_none()
        if not u:
          raise HTTPException(status_code=404, detail="User not found")

        u.is_active = is_active
        if new_pw_raw:
          u.password_hash = hash_password_pbkdf2(new_pw_raw)
        await s.commit()
      else:
        # If email exists -> update it (password optional).
        if not email_raw:
          raise HTTPException(status_code=400, detail="email is required")

        u = (await s.execute(select(AdminUser).where(AdminUser.email == email_raw))).scalar_one_or_none()
        if u:
          u.is_active = is_active
          if new_pw_raw:
            u.password_hash = hash_password_pbkdf2(new_pw_raw)
          await s.commit()
        else:
          # Create new user (requires password).
          if not new_pw_raw:
            raise HTTPException(status_code=400, detail="password is required to create user")
          pwd_hash = hash_password_pbkdf2(new_pw_raw)
          s.add(AdminUser(email=email_raw, password_hash=pwd_hash, is_active=is_active))
          await s.commit()

  # sessions: read-only for now

  return RedirectResponse(url=f"/settings?tab={tab}&saved=1", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, _: bool = Depends(require_auth)):
  async with SessionLocal() as s:
    total_vacancies = (await s.execute(select(func.count(Vacancy.id)))).scalar() or 0
    total_channels = (await s.execute(select(func.count(Channel.id)))).scalar() or 0
    
    # Статистика по категориям
    category_stats = (
      await s.execute(
        select(Vacancy.category, func.count(Vacancy.id).label("count"))
        .group_by(Vacancy.category)
      )
    ).all()
    
    # Последние 20 вакансий (упрощённые карточки)
    last_vacancies = (
      await s.execute(
        select(Vacancy)
        .order_by(desc(Vacancy.id))
        .limit(20)
      )
    ).scalars().all()

  return templates.TemplateResponse(
    "dashboard.html",
    {
      "request": request,
      "now": datetime.utcnow(),
      "total_vacancies": total_vacancies,
      "total_channels": total_channels,
      "category_stats": category_stats,
      "last_vacancies": last_vacancies,
    },
  )


@app.get("/vacancies", response_class=HTMLResponse)
async def vacancies_page(
  request: Request,
  _: bool = Depends(require_auth),
  page: int = Query(1, ge=1),
  per_page: int = Query(50, ge=1, le=200),
  category: str | None = Query(None),
  channel: str | None = Query(None),
  search: str | None = Query(None),
):
  async with SessionLocal() as s:
    query = select(Vacancy)
    
    if category:
      query = query.where(Vacancy.category == category)
    if channel:
      query = query.where(Vacancy.tg_channel_username == channel)
    if search:
      search_filter = or_(
        Vacancy.title.ilike(f"%{search}%"),
        Vacancy.company_name.ilike(f"%{search}%"),
        Vacancy.raw_text.ilike(f"%{search}%"),
      )
      query = query.where(search_filter)
    
    total = (await s.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
    
    vacancies = (
      await s.execute(
        query
        .order_by(desc(Vacancy.id))
        .offset((page - 1) * per_page)
        .limit(per_page)
      )
    ).scalars().all()
    
    categories = (
      await s.execute(
        select(Vacancy.category, func.count(Vacancy.id).label("count"))
        .group_by(Vacancy.category)
        .order_by(desc("count"))
      )
    ).all()
    
    channels = (
      await s.execute(
        select(Vacancy.tg_channel_username, func.count(Vacancy.id).label("count"))
        .where(Vacancy.tg_channel_username.isnot(None))
        .group_by(Vacancy.tg_channel_username)
        .order_by(desc("count"))
      )
    ).all()

  return templates.TemplateResponse(
    "vacancies.html",
    {
      "request": request,
      "vacancies": vacancies,
      "total": total,
      "page": page,
      "per_page": per_page,
      "total_pages": (total + per_page - 1) // per_page if total > 0 else 1,
      "category": category,
      "channel": channel,
      "search": search,
      "categories": categories,
      "channels": channels,
    },
  )


@app.get("/api/vacancies/{vacancy_id}")
async def get_vacancy_details(vacancy_id: int, _: bool = Depends(require_auth)):
  async with SessionLocal() as s:
    v = (await s.execute(select(Vacancy).where(Vacancy.id == vacancy_id))).scalar_one_or_none()
    if not v:
      raise HTTPException(status_code=404, detail="Vacancy not found")
    return {
      "id": v.id,
      "title": v.title,
      "company_name": v.company_name,
      "recruiter": getattr(v, "recruiter", None),
      "domains": getattr(v, "domains", []) or ([]),
      "risk_label": getattr(v, "risk_label", None),
      "ai_score_value": getattr(v, "ai_score_value", None),
      "stack": getattr(v, "stack", []) or [],
      "summary_en": getattr(v, "summary_en", None),
      "location_type": v.location_type,
      "salary_min_usd": v.salary_min_usd,
      "salary_max_usd": v.salary_max_usd,
      "contacts": getattr(v, "contacts", []) or [],
      "description": getattr(v, "description", None),
      "responsibilities": getattr(v, "responsibilities", None),
      "requirements": getattr(v, "requirements", None),
      "conditions": getattr(v, "conditions", None),
      "raw_text": v.raw_text,
      "source_url": v.source_url,
      "created_at": v.created_at.isoformat() if v.created_at else None,
    }


@app.post("/api/vacancies/{vacancy_id}/delete")
async def delete_vacancy(vacancy_id: int, _: bool = Depends(require_auth)):
  async with SessionLocal() as s:
    await s.execute(delete(Vacancy).where(Vacancy.id == vacancy_id))
    await s.commit()
  return {"success": True}


@app.post("/api/vacancies/{vacancy_id}/reanalyze")
async def reanalyze_vacancy(vacancy_id: int, _: bool = Depends(require_auth)):
  """
  Re-run AI analysis for an existing vacancy based on raw_text and update structured fields.
  Useful for backfilling old rows where blocks/summary were empty or in RU.
  """
  async with SessionLocal() as s:
    v = (await s.execute(select(Vacancy).where(Vacancy.id == vacancy_id))).scalar_one_or_none()
    if not v:
      raise HTTPException(status_code=404, detail="Vacancy not found")
    text_raw = (v.raw_text or "").strip()
    if not text_raw:
      raise HTTPException(status_code=400, detail="Vacancy has no raw_text")

  try:
    analysis = await analyze_with_openrouter(text_raw)
  except httpx.HTTPStatusError as e:
    # Most common case: OpenRouter billing/limit errors (e.g. 402 Payment Required)
    upstream_status = int(getattr(e.response, "status_code", 502) or 502)
    upstream_body = ""
    try:
      upstream_body = e.response.text
    except Exception:
      upstream_body = ""

    msg = f"OpenRouter error {upstream_status}"
    if upstream_body:
      msg += f": {upstream_body[:2000]}"

    # Pass through only user-meaningful statuses; otherwise map to 502
    status_code = upstream_status if upstream_status in (400, 401, 402, 403, 429) else 502
    raise HTTPException(status_code=status_code, detail=msg)

  async with SessionLocal() as s:
    await s.execute(
      update(Vacancy)
      .where(Vacancy.id == vacancy_id)
      .values(
        company_name=analysis.get("company_name"),
        title=analysis.get("title"),
        location_type=analysis.get("location_type"),
        salary_min_usd=analysis.get("salary_min_usd"),
        salary_max_usd=analysis.get("salary_max_usd"),
        stack=analysis.get("stack") or [],
        ai_score_value=analysis.get("ai_score_value"),
        summary_en=analysis.get("summary_en"),
        summary_ru=analysis.get("summary_ru"),
        domains=[str(x).strip().lower() for x in (analysis.get("domains") or []) if str(x).strip()],
        risk_label=analysis.get("risk_label"),
        recruiter=analysis.get("recruiter"),
        contacts=analysis.get("contacts") or [],
        description=analysis.get("description"),
        responsibilities=analysis.get("responsibilities"),
        requirements=analysis.get("requirements"),
        conditions=analysis.get("conditions"),
        role=analysis.get("role"),
        seniority=analysis.get("seniority"),
        standardized_title=analysis.get("standardized_title"),
        language=analysis.get("language"),
      )
    )
    await s.commit()

  return {"success": True}


@app.get("/channels", response_class=HTMLResponse)
async def channels_page(
  request: Request,
  _: bool = Depends(require_auth),
  search: str | None = Query(None),
  enabled: str | None = Query(None),
  domain: str | None = Query(None),
  risk: str | None = Query(None),
  sort: str | None = Query("created_desc"),
):
  async with SessionLocal() as s:
    query = (
      select(Channel, func.count(Vacancy.id).label("vacancies_count"))
      .outerjoin(Vacancy, Channel.username == Vacancy.tg_channel_username)
      .group_by(Channel.id)
    )

    if search:
      s_like = f"%{search.strip()}%"
      query = query.where(
        or_(
          Channel.username.ilike(s_like),
          Channel.title.ilike(s_like),
          Channel.bio.ilike(s_like),
        )
      )

    if enabled in ("true", "false"):
      query = query.where(Channel.enabled == (enabled == "true"))

    if domain:
      # ai_domains is text[] in DB
      query = query.where(Channel.ai_domains.contains([domain.strip().lower()]))

    if risk == "high-risk":
      query = query.where(Channel.ai_risk_label == "high-risk")
    elif risk == "not-high-risk":
      query = query.where(or_(Channel.ai_risk_label.is_(None), Channel.ai_risk_label != "high-risk"))

    if sort == "members_desc":
      query = query.order_by(desc(Channel.members_count.nullslast()), desc(Channel.created_at))
    elif sort == "members_asc":
      query = query.order_by(Channel.members_count.asc().nullslast(), desc(Channel.created_at))
    elif sort == "vacancies_desc":
      query = query.order_by(desc(text("vacancies_count")), desc(Channel.created_at))
    else:
      query = query.order_by(desc(Channel.created_at))

    channels = (await s.execute(query)).all()

    # domain options
    domain_rows = (
      await s.execute(
        text(
          "SELECT DISTINCT d AS domain "
          "FROM channels c, unnest(c.ai_domains) d "
          "WHERE d IS NOT NULL AND d <> '' "
          "ORDER BY d ASC"
        )
      )
    ).all()
    domain_options = [r[0] for r in domain_rows]

  return templates.TemplateResponse(
    "channels.html",
    {
      "request": request,
      "channels": channels,
      "filters": {
        "search": search or "",
        "enabled": enabled or "",
        "domain": domain or "",
        "risk": risk or "",
        "sort": sort or "created_desc",
      },
      "domain_options": domain_options,
    },
  )


async def _get_telethon_client() -> TelegramClient:
  if not settings.telethon_api_id or not settings.telethon_api_hash:
    raise HTTPException(status_code=500, detail="TELETHON_API_ID/TELETHON_API_HASH not configured")
  client = TelegramClient(settings.telethon_session_path, settings.telethon_api_id, settings.telethon_api_hash)
  await client.connect()
  if not await client.is_user_authorized():
    await client.disconnect()
    raise HTTPException(status_code=500, detail="Telethon session not authorized")
  return client


@app.get("/api/channels/{channel_id}")
async def get_channel_details(channel_id: int, _: bool = Depends(require_auth)):
  async with SessionLocal() as s:
    ch = (await s.execute(select(Channel).where(Channel.id == channel_id))).scalar_one_or_none()
    if not ch:
      raise HTTPException(status_code=404, detail="Channel not found")
    latest = (
      await s.execute(
        select(Vacancy)
        .where(or_(Vacancy.tg_channel_id == ch.tg_id, Vacancy.tg_channel_username == ch.username, Vacancy.source_channel == ch.username))
        .order_by(desc(Vacancy.id))
        .limit(10)
      )
    ).scalars().all()
  return {
    "id": ch.id,
    "username": ch.username,
    "title": ch.title,
    "bio": ch.bio,
    "members_count": ch.members_count,
    "enabled": ch.enabled,
    "ai_domains": getattr(ch, "ai_domains", []) or [],
    "ai_tags": getattr(ch, "ai_tags", []) or [],
    "ai_risk_label": getattr(ch, "ai_risk_label", None),
    "latest_vacancies": [{"id": v.id, "title": v.title, "company_name": v.company_name, "created_at": v.created_at.isoformat() if v.created_at else None} for v in latest],
  }


@app.post("/api/channels/{channel_id}/refresh-ai")
async def refresh_channel_ai(channel_id: int, _: bool = Depends(require_auth)):
  async with SessionLocal() as s:
    ch = (await s.execute(select(Channel).where(Channel.id == channel_id))).scalar_one_or_none()
    if not ch:
      raise HTTPException(status_code=404, detail="Channel not found")

  client = await _get_telethon_client()
  try:
    # Resolve entity
    entity = None
    if ch.username:
      entity = await client.get_entity(ch.username)
    elif ch.tg_id:
      entity = await client.get_entity(ch.tg_id)
    else:
      raise HTTPException(status_code=400, detail="Channel has no username/tg_id")

    full = await client(GetFullChannelRequest(entity))
    bio = getattr(full.full_chat, "about", None)
    members = getattr(full.full_chat, "participants_count", None)
    title = getattr(entity, "title", None) or ch.title
    username = getattr(entity, "username", None) or ch.username

    # last 3 posts
    posts: list[str] = []
    async for m in client.iter_messages(entity, limit=3):
      if getattr(m, "message", None):
        posts.append(m.message)

    ai = await categorize_channel(title=title, bio=bio, last_posts=posts)
    # store contacts from bio into tags (since we don't have a dedicated column)
    tags = list(ai.get("ai_tags") or [])
    for c in (ai.get("admin_contacts") or []):
      tags.append(f"contact:{c}")
    tags = list(dict.fromkeys(tags))  # stable unique

    async with SessionLocal() as s:
      await s.execute(
        update(Channel)
        .where(Channel.id == ch.id)
        .values(
          tg_id=getattr(entity, "id", ch.tg_id),
          username=username,
          title=title,
          bio=bio,
          members_count=members,
          ai_domains=ai.get("ai_domains", []),
          ai_tags=tags,
          ai_risk_label=ai.get("ai_risk_label"),
        )
      )
      await s.commit()
  finally:
    await client.disconnect()
  return {"success": True}


@app.post("/api/channels/{channel_id}/fetch-last-5")
async def fetch_last_5_posts(channel_id: int, _: bool = Depends(require_auth)):
  async with SessionLocal() as s:
    ch = (await s.execute(select(Channel).where(Channel.id == channel_id))).scalar_one_or_none()
    if not ch:
      raise HTTPException(status_code=404, detail="Channel not found")

  client = await _get_telethon_client()
  saved = 0
  scanned = 0
  try:
    if ch.username:
      entity = await client.get_entity(ch.username)
    elif ch.tg_id:
      entity = await client.get_entity(ch.tg_id)
    else:
      raise HTTPException(status_code=400, detail="Channel has no username/tg_id")

    async for m in client.iter_messages(entity, limit=5):
      if not getattr(m, "message", None):
        continue
      scanned += 1
      msg_id = getattr(m, "id", None)
      uname = getattr(entity, "username", None) or ch.username
      url = f"https://t.me/{uname}/{msg_id}" if uname and msg_id else None
      ok = await process_text_message(
        text_raw=m.message,
        tg_message_id=msg_id,
        tg_channel_id=getattr(entity, "id", ch.tg_id),
        tg_channel_username=uname,
        source_url=url,
      )
      if ok:
        saved += 1
  finally:
    await client.disconnect()
  return {"success": True, "scanned": scanned, "saved": saved}


@app.post("/api/channels/bulk/refresh-ai")
async def bulk_refresh_ai(payload: dict, _: bool = Depends(require_auth)):
  ids = payload.get("ids") or []
  updated = 0
  for cid in ids:
    try:
      await refresh_channel_ai(int(cid), _)
      updated += 1
    except Exception:
      continue
  return {"success": True, "updated": updated}


@app.post("/api/channels/bulk/fetch-last-5")
async def bulk_fetch_last_5(payload: dict, _: bool = Depends(require_auth)):
  ids = payload.get("ids") or []
  total_scanned = 0
  total_saved = 0
  for cid in ids:
    try:
      res = await fetch_last_5_posts(int(cid), _)
      total_scanned += int(res.get("scanned") or 0)
      total_saved += int(res.get("saved") or 0)
    except Exception:
      continue
  return {"success": True, "scanned": total_scanned, "saved": total_saved}


@app.post("/api/channels/bulk/toggle-enabled")
async def bulk_toggle_enabled(payload: dict, _: bool = Depends(require_auth)):
  ids = payload.get("ids") or []
  enabled = payload.get("enabled")
  if enabled not in (True, False, "true", "false"):
    raise HTTPException(status_code=400, detail="enabled must be true/false")
  enabled_bool = enabled is True or enabled == "true"
  async with SessionLocal() as s:
    await s.execute(update(Channel).where(Channel.id.in_([int(x) for x in ids])).values(enabled=enabled_bool))
    await s.commit()
  return {"success": True, "enabled": enabled_bool, "updated": len(ids)}


@app.post("/api/channels/bulk/normalize-ai")
async def bulk_normalize_ai(payload: dict, _: bool = Depends(require_auth)):
  """
  Normalize existing ai_domains/ai_tags values (lowercase + dedupe) to fix search/filtering.
  If ids omitted -> normalize all channels.
  """
  ids = payload.get("ids")
  async with SessionLocal() as s:
    if ids:
      rows = (await s.execute(select(Channel).where(Channel.id.in_([int(x) for x in ids])))).scalars().all()
    else:
      rows = (await s.execute(select(Channel))).scalars().all()
    for ch in rows:
      domains = [str(x).strip().lower() for x in (getattr(ch, "ai_domains", []) or []) if str(x).strip()]
      tags = [str(x).strip() for x in (getattr(ch, "ai_tags", []) or []) if str(x).strip()]
      # normalize tags too (keep case-insensitive uniqueness)
      tags_norm = []
      seen = set()
      for t in tags:
        k = t.lower()
        if k in seen:
          continue
        seen.add(k)
        tags_norm.append(t)
      ch.ai_domains = sorted(set(domains))
      ch.ai_tags = tags_norm[:3]  # keep UI sane
    await s.commit()
  return {"success": True, "normalized": len(rows)}


@app.post("/api/channels/{channel_id}/toggle")
async def toggle_channel(channel_id: int, _: bool = Depends(require_auth)):
  async with SessionLocal() as s:
    channel = (await s.execute(select(Channel).where(Channel.id == channel_id))).scalar_one_or_none()
    if not channel:
      raise HTTPException(status_code=404, detail="Channel not found")
    
    new_status = not channel.enabled
    await s.execute(
      update(Channel).where(Channel.id == channel_id).values(enabled=new_status)
    )
    await s.commit()
    return {"enabled": new_status}


@app.post("/api/channels/{channel_id}/delete")
async def delete_channel(channel_id: int, _: bool = Depends(require_auth)):
  async with SessionLocal() as s:
    await s.execute(delete(Channel).where(Channel.id == channel_id))
    await s.commit()
    return {"success": True}


@app.get("/api/channels/{channel_id}/last-post")
async def get_last_post(channel_id: int, _: bool = Depends(require_auth)):
  async with SessionLocal() as s:
    channel = (await s.execute(select(Channel).where(Channel.id == channel_id))).scalar_one_or_none()
    if not channel:
      raise HTTPException(status_code=404, detail="Channel not found")
    
    last_vacancy = (
      await s.execute(
        select(Vacancy)
        .where(Vacancy.tg_channel_username == channel.username)
        .order_by(desc(Vacancy.id))
        .limit(1)
      )
    ).scalar_one_or_none()
    
    if not last_vacancy:
      return {"message": "No posts found"}
    
    return {
      "id": last_vacancy.id,
      "title": last_vacancy.title,
      "raw_text": last_vacancy.raw_text,
      "created_at": last_vacancy.created_at.isoformat() if last_vacancy.created_at else None,
    }


@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request, _: bool = Depends(require_auth)):
  async with SessionLocal() as s:
    category_stats = (
      await s.execute(
        select(Vacancy.category, func.count(Vacancy.id).label("count"))
        .group_by(Vacancy.category)
        .order_by(desc("count"))
      )
    ).all()
    
    channel_stats = (
      await s.execute(
        select(
          Vacancy.tg_channel_username,
          func.count(Vacancy.id).label("count")
        )
        .where(Vacancy.tg_channel_username.isnot(None))
        .group_by(Vacancy.tg_channel_username)
        .order_by(desc("count"))
        .limit(20)
      )
    ).all()
    
    date_stats = (
      await s.execute(
        select(
          func.date(Vacancy.created_at).label("date"),
          func.count(Vacancy.id).label("count")
        )
        # NOTE: func.interval("30 days") becomes interval($1) in Postgres and breaks.
        .where(text("vacancies.created_at >= (CURRENT_DATE - INTERVAL '30 days')"))
        .group_by(func.date(Vacancy.created_at))
        .order_by(desc("date"))
      )
    ).all()
    
    salary_stats = (
      await s.execute(
        select(
          Vacancy.category,
          func.avg(Vacancy.salary_min_usd).label("avg_min"),
          func.avg(Vacancy.salary_max_usd).label("avg_max"),
          func.count(Vacancy.id).label("count")
        )
        .where(
          or_(
            Vacancy.salary_min_usd.isnot(None),
            Vacancy.salary_max_usd.isnot(None)
          )
        )
        .group_by(Vacancy.category)
      )
    ).all()

  return templates.TemplateResponse(
    "analytics.html",
    {
      "request": request,
      "category_stats": category_stats,
      "channel_stats": channel_stats,
      "date_stats": date_stats,
      "salary_stats": salary_stats,
    },
  )

from __future__ import annotations
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import secrets

from markupsafe import Markup

from fastapi import FastAPI, Request, Query, Form, Depends, HTTPException, status, Header, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
import httpx
from sqlalchemy import select, func, desc, or_, update, delete, text, case
from starlette.exceptions import HTTPException as StarletteHTTPException
from telethon import TelegramClient
from telethon.tl.functions.channels import GetFullChannelRequest

from jobeco.db.session import SessionLocal, engine
from jobeco.db.models import Vacancy, Channel, SystemSettings, AdminUser, ApiKey, ApiKeyUsage, ParserLog, Company, WebSource, DocArticle
from jobeco.settings import settings
from jobeco.openrouter.client import categorize_channel, analyze_with_openrouter, embed_text, score_vacancy_with_openrouter, resolve_company_info, enrich_company_profile
from jobeco.processing.pipeline import upsert_company, _boost_company_score, try_enrich_from_ats, _strip_channel_from_contacts
from jobeco.processing.company_branding import pick_corporate_website
from jobeco.processing.pipeline import process_text_message
from jobeco.runtime_settings import (
  get_runtime_settings,
  upsert_system_settings,
  load_system_settings_raw,
)
from jobeco.auth.passwords import hash_password_pbkdf2, verify_password_pbkdf2


import asyncio as _asyncio
from contextlib import asynccontextmanager

from jobeco.parsers.degencryptojobs import sync_source as degen_sync, ensure_source_record as degen_ensure
from jobeco.parsers.web3career import sync_source as w3c_sync, ensure_source_record as w3c_ensure
from jobeco.parsers.cryptojobs import sync_source as cj_sync, ensure_source_record as cj_ensure
from jobeco.parsers.remocate import sync_source as remo_sync, ensure_source_record as remo_ensure
from jobeco.parsers.cryptocurrencyjobs_co import sync_source as ccj_sync, ensure_source_record as ccj_ensure
from jobeco.parsers.sailonchain import sync_source as sail_sync, ensure_source_record as sail_ensure
from jobeco.parsers.findweb3 import sync_source as fw3_sync, ensure_source_record as fw3_ensure
from jobeco.db.base import Base as _SABase
import structlog as _structlog

_bg_log = _structlog.get_logger()


async def _web_sources_scheduler():
  """Background loop: sync enabled web sources on their configured interval."""
  await _asyncio.sleep(10)  # let the app finish startup
  while True:
    try:
      async with SessionLocal() as s:
        sources = (await s.execute(
          select(WebSource).where(WebSource.enabled == True)
        )).scalars().all()
      for src in sources:
        if src.last_synced_at:
          elapsed = (datetime.now(timezone.utc) - src.last_synced_at).total_seconds()
          if elapsed < src.sync_interval_minutes * 60:
            continue
        _bg_log.info("web_source_sync_start", slug=src.slug)
        try:
          if src.parser_type == "degencryptojobs":
            await degen_sync(max_pages=src.max_pages, limit=20)
          elif src.parser_type == "web3career":
            await w3c_sync(max_pages=src.max_pages, limit=20)
          elif src.parser_type == "cryptojobs":
            await cj_sync(max_pages=src.max_pages, limit=10)
          elif src.parser_type == "remocate":
            await remo_sync(max_pages=src.max_pages, limit=15)
          elif src.parser_type == "cryptocurrencyjobs_co":
            await ccj_sync(max_pages=src.max_pages, limit=25)
          elif src.parser_type == "sailonchain":
            await sail_sync(max_pages=src.max_pages, limit=20)
          elif src.parser_type == "findweb3":
            await fw3_sync(max_pages=src.max_pages, limit=20)
        except Exception as e:
          _bg_log.error("web_source_sync_error", slug=src.slug, error=str(e))
    except Exception as e:
      _bg_log.error("scheduler_loop_error", error=str(e))
    await _asyncio.sleep(300)


@asynccontextmanager
async def lifespan(app):
  # Create web_sources table if missing
  async with engine.begin() as conn:
    await conn.run_sync(lambda c: _SABase.metadata.create_all(c, tables=[WebSource.__table__], checkfirst=True))
  await degen_ensure()
  await w3c_ensure()
  await cj_ensure()
  await remo_ensure()
  await ccj_ensure()
  await sail_ensure()
  await fw3_ensure()
  task = _asyncio.create_task(_web_sources_scheduler())
  yield
  task.cancel()


app = FastAPI(title="Job-Eco Admin", lifespan=lifespan, docs_url="/api/swagger", redoc_url="/api/redoc")
# Stable secret key is required for sessions to survive restarts.
_session_secret = settings.session_secret_key or settings.openrouter_api_key or "jobeco_session_dev_secret"
app.add_middleware(SessionMiddleware, secret_key=_session_secret)

templates = Jinja2Templates(directory="templates")
templates.env.globals["company_url"] = lambda name, cid: (
    f"/companies/{_vacancy_slug(name or 'company')}-{cid}" if cid else ""
)

from starlette.staticfiles import StaticFiles
import os as _os
_static_dir = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "static")
if _os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

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


def _is_authenticated(request: Request) -> bool:
  """Check if current session is authenticated (non-blocking)."""
  return bool(request.session.get("authenticated") and request.session.get("user_id"))


def _hash_api_key_token(token: str) -> str:
  return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _generate_api_key_token() -> str:
  # URL-safe token; it will be shown once (because we store only its hash).
  return secrets.token_urlsafe(32)


def _parse_csv_list(value: str | None) -> list[str]:
  if not value:
    return []
  items: list[str] = []
  for x in value.split(","):
    v = (x or "").strip().lower()
    if v:
      items.append(v)
  # De-duplicate preserving order.
  seen: set[str] = set()
  out: list[str] = []
  for v in items:
    if v not in seen:
      seen.add(v)
      out.append(v)
  return out


import re as _re

_FORM_URL_RE = _re.compile(
  r'https?://(?:'
  r'forms\.gle/[A-Za-z0-9]+|'
  r'docs\.google\.com/forms/[^\s)\"\'<>]+|'
  r'[a-z0-9-]+\.typeform\.com/[^\s)\"\'<>]+|'
  r'(?:www\.)?jotform\.com/[^\s)\"\'<>]+|'
  r'tally\.so/[^\s)\"\'<>]+|'
  r'airtable\.com/shr[^\s)\"\'<>]+|'
  r'(?:www\.)?surveymonkey\.com/[^\s)\"\'<>]+|'
  r'jobs\.lever\.co/[^\s)\"\'<>]+|'
  r'boards\.greenhouse\.io/[^\s)\"\'<>]+|'
  r'apply\.workable\.com/[^\s)\"\'<>]+|'
  r'[a-z0-9-]+\.breezy\.hr/[^\s)\"\'<>]+|'
  r'[a-z0-9-]+\.bamboohr\.com/[^\s)\"\'<>]+|'
  r'jobs\.smartrecruiters\.com/[^\s)\"\'<>]+|'
  r'jobs\.ashbyhq\.com/[^\s)\"\'<>]+|'
  r'[a-z0-9-]+\.recruitee\.com/[^\s)\"\'<>]+'
  r')',
  _re.IGNORECASE,
)


def _safe_int(val) -> int | None:
  if val is None:
    return None
  try:
    s = str(val).strip()
    return int(s) if s else None
  except Exception:
    return None


def _enrich_contacts_with_forms(contacts: list[str], raw_text: str | None) -> list[str]:
  """Append application-form URLs found in raw_text that the LLM missed."""
  if not raw_text:
    return contacts
  existing_lower = {c.lower() for c in contacts}
  for m in _FORM_URL_RE.finditer(raw_text):
    url = m.group(0)
    if url.lower() not in existing_lower:
      contacts.append(url)
      existing_lower.add(url.lower())
  return contacts


def _contacts_list_to_dict(contacts: list[str] | None) -> dict[str, str]:
  """
  Convert stored contacts (list[str] from LLM) into a typed dict for API consumers.

  Note: the DB stores only raw strings; we apply simple heuristics to classify.
  """
  out: dict[str, str] = {}
  for raw in contacts or []:
    if raw is None:
      continue
    c = str(raw).strip()
    if not c:
      continue

    lc = c.lower()
    label = "Other"
    val = c

    form_domains = ("forms.gle/", "docs.google.com/forms", "typeform.com", "jotform.com",
                     "tally.so", "airtable.com/shr", "surveymonkey.com", "notion.so")

    if lc.startswith("mailto:"):
      label = "Email"
      val = c[7:].strip()
    elif c.startswith("@"):
      label = "Telegram"
      val = c
    elif lc.startswith("t.me/"):
      label = "Telegram"
      val = c
    elif lc.startswith("http://") or lc.startswith("https://"):
      if any(fd in lc for fd in form_domains):
        label = "Application Form"
      elif "linkedin.com" in lc:
        label = "LinkedIn"
      elif "t.me/" in lc:
        label = "Telegram"
      else:
        label = "URL"
    else:
      # Very rough email/phone detection (kept intentionally simple for MVP).
      if "@" in c and "." in c and not c.startswith("@"):
        label = "Email"
      elif any(ch.isdigit() for ch in c) and len(c) >= 7:
        label = "Phone"

    if label in out:
      # Keep stable formatting for multiple contacts of the same type.
      if val not in out[label].split(", "):
        out[label] = out[label] + ", " + val
    else:
      out[label] = val

  return out


async def _enforce_api_key_limits_and_log(
  *,
  s,
  api_key: ApiKey,
  endpoint: str,
  status_code: int,
):
  """
  Логирует usage и проверяет ограничения.

  Важно: это простая реализация через COUNT по окнам (1 мин / 24 часа).
  Для прод-среды при высокой нагрузке стоит заменить на Redis-RateLimit.
  """
  limits = api_key.limits or {}
  now = datetime.utcnow()

  requests_per_minute = limits.get("requests_per_minute")
  daily_quota = limits.get("daily_quota")

  # Будем логировать только если лимиты не превышены или если это уже ошибка 429.
  if status_code != 429:
    minute_start = now - timedelta(minutes=1)
    day_start = now - timedelta(days=1)

    q_min = await s.execute(
      select(func.count())
      .select_from(ApiKeyUsage)
      .where(
        ApiKeyUsage.api_key_id == api_key.id,
        ApiKeyUsage.requested_at >= minute_start,
      )
    )
    used_min = q_min.scalar() or 0
    if requests_per_minute is not None:
      try:
        requests_per_minute_int = int(requests_per_minute)
      except Exception:
        requests_per_minute_int = None
      if requests_per_minute_int is not None and used_min >= requests_per_minute_int:
        # Логируем 429 и возвращаем ошибку.
        s.add(
          ApiKeyUsage(
            api_key_id=api_key.id,
            endpoint=endpoint,
            status_code=429,
            requested_at=now,
          )
        )
        await s.commit()
        raise HTTPException(status_code=429, detail="API rate limit exceeded")

    q_day = await s.execute(
      select(func.count())
      .select_from(ApiKeyUsage)
      .where(
        ApiKeyUsage.api_key_id == api_key.id,
        ApiKeyUsage.requested_at >= day_start,
      )
    )
    used_day = q_day.scalar() or 0
    if daily_quota is not None:
      try:
        daily_quota_int = int(daily_quota)
      except Exception:
        daily_quota_int = None
      if daily_quota_int is not None and used_day >= daily_quota_int:
        s.add(
          ApiKeyUsage(
            api_key_id=api_key.id,
            endpoint=endpoint,
            status_code=429,
            requested_at=now,
          )
        )
        await s.commit()
        raise HTTPException(status_code=429, detail="API daily quota exceeded")

  # Логируем реальную попытку (успех или внешняя обработка ошибок).
  s.add(
    ApiKeyUsage(
      api_key_id=api_key.id,
      endpoint=endpoint,
      status_code=status_code,
      requested_at=now,
    )
  )
  await s.commit()


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
  if request.url.path.startswith("/api/public/"):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
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
  return templates.TemplateResponse(request, "login.html")


@app.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
  email = (email or "").strip().lower()
  if not email or not password:
    return templates.TemplateResponse(
      request, "login.html",
      {"error": "Введите email и пароль"},
      status_code=401,
    )

  async with SessionLocal() as s:
    u = (await s.execute(select(AdminUser).where(AdminUser.email == email))).scalar_one_or_none()
    if not u or not u.is_active:
      return templates.TemplateResponse(
        request, "login.html",
        {"error": "Неверный email или пароль"},
        status_code=401,
      )

    if not verify_password_pbkdf2(password, u.password_hash):
      return templates.TemplateResponse(
        request, "login.html",
        {"error": "Неверный email или пароль"},
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
    request, "settings.html",
    {
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


@app.get("/about", response_class=HTMLResponse)
async def about_page(request: Request):
  """About page with SEO meta."""
  async with SessionLocal() as s:
    vac_count = (await s.execute(select(func.count(Vacancy.id)))).scalar() or 0
    comp_count = (await s.execute(select(func.count(Company.id)))).scalar() or 0
    tg_ch = (await s.execute(select(func.count(Channel.id)))).scalar() or 0
    ws_cnt = 0
    try:
      ws_cnt = (await s.execute(select(func.count(WebSource.id)))).scalar() or 0
    except Exception:
      pass
    src_count = tg_ch + ws_cnt
    dom_count = 0
    try:
      dom_count = (await s.execute(text("SELECT COUNT(DISTINCT d) FROM vacancies CROSS JOIN LATERAL unnest(domains) AS d WHERE d IS NOT NULL"))).scalar() or 0
    except Exception:
      pass
  return templates.TemplateResponse(request, "about.html", {
    "stats": {"vacancies": vac_count, "companies": comp_count, "sources": src_count, "domains": dom_count},
  })


@app.get("/landing", response_class=HTMLResponse)
async def landing_page(request: Request):
  """Public landing page for HireLens."""
  async with SessionLocal() as s:
    total_vacancies = (await s.execute(select(func.count(Vacancy.id)))).scalar() or 0
    tg_channels = (await s.execute(select(func.count(Channel.id)))).scalar() or 0
    web_sources_cnt = 0
    try:
      web_sources_cnt = (await s.execute(select(func.count(WebSource.id)))).scalar() or 0
    except Exception:
      pass
    total_sources = tg_channels + web_sources_cnt
    total_companies = (await s.execute(select(func.count(Company.id)))).scalar() or 0
    avg_score = (await s.execute(select(func.round(func.avg(Vacancy.ai_score_value), 1)).where(
      Vacancy.ai_score_value.isnot(None)
    ))).scalar()
    domain_stats_rows = (await s.execute(text(
      """SELECT lower(btrim(d)) as category, count(*) as count
         FROM vacancies v CROSS JOIN LATERAL unnest(v.domains) as d
         WHERE d IS NOT NULL AND btrim(d) <> ''  GROUP BY 1 ORDER BY count DESC LIMIT 15"""
    ))).all()
    domains = [(r.category, r.count) for r in domain_stats_rows]
  return templates.TemplateResponse(
    request, "landing.html",
    {
      "stats": {
        "total_vacancies": f"{total_vacancies:,}",
        "total_companies": f"{total_companies:,}",
        "total_sources": total_sources,
        "avg_score": avg_score or "—",
        "tg_channels": tg_channels,
        "domains": domains,
      },
      "blog_posts": BLOG_POSTS[:3],
    },
  )


@app.get("/api/landing/search")
async def landing_search(
  q: str = Query("", min_length=0),
  limit: int = Query(12, ge=1, le=30),
):
  """Fast vacancy search for the landing page — semantic + keyword, no API key."""
  term = (q or "").strip()
  if len(term) < 2:
    return {"items": []}

  results = []
  async with SessionLocal() as s:
    embedding = None
    try:
      embedding = await embed_text(term)
    except Exception:
      pass

    if embedding:
      vec = "[" + ",".join(f"{x:.6f}" for x in embedding) + "]"
      sql = text("""
        SELECT v.id, v.title, v.company_name, v.domains, v.role, v.seniority,
               v.salary_min_usd, v.salary_max_usd, v.ai_score_value,
               v.location_type, v.created_at, v.source_url, v.company_url,
               c.logo_url,
               1 - (v.embedding <=> (:vec)::vector) AS similarity
        FROM vacancies v
        LEFT JOIN companies c ON c.id = v.company_id
        WHERE v.embedding IS NOT NULL
        ORDER BY v.embedding <=> (:vec)::vector
        LIMIT :lim
      """)
      rows = (await s.execute(sql, {"vec": vec, "lim": limit})).mappings().all()
      for r in rows:
        results.append({
          "id": r["id"], "title": r["title"], "company_name": r["company_name"],
          "domains": r["domains"] or [], "role": r["role"], "seniority": r["seniority"],
          "salary_min": r["salary_min_usd"], "salary_max": r["salary_max_usd"],
          "score": float(r["ai_score_value"]) if r["ai_score_value"] else None,
          "location_type": r["location_type"],
          "created_at": r["created_at"].isoformat() if r["created_at"] else None,
          "source_url": r["source_url"], "company_url": r["company_url"],
          "logo_url": r["logo_url"],
          "similarity": round(float(r["similarity"]), 3) if r["similarity"] else None,
        })
    else:
      like = f"%{term}%"
      rows = (await s.execute(
        select(
          Vacancy.id, Vacancy.title, Vacancy.company_name, Vacancy.domains,
          Vacancy.role, Vacancy.seniority,
          Vacancy.salary_min_usd, Vacancy.salary_max_usd, Vacancy.ai_score_value,
          Vacancy.location_type, Vacancy.created_at, Vacancy.source_url, Vacancy.company_url,
        )
        .where(or_(Vacancy.title.ilike(like), Vacancy.company_name.ilike(like), Vacancy.raw_text.ilike(like)))
        .order_by(desc(Vacancy.id))
        .limit(limit)
      )).all()
      for r in rows:
        results.append({
          "id": r.id, "title": r.title, "company_name": r.company_name,
          "domains": r.domains or [], "role": r.role, "seniority": r.seniority,
          "salary_min": r.salary_min_usd, "salary_max": r.salary_max_usd,
          "score": float(r.ai_score_value) if r.ai_score_value else None,
          "location_type": r.location_type,
          "created_at": r.created_at.isoformat() if r.created_at else None,
          "source_url": r.source_url, "company_url": r.company_url,
          "logo_url": None,
        })

  return {"items": results}



@app.get("/favicon.svg", include_in_schema=False)
async def favicon():
    import os
    fpath = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "favicon.svg")
    from starlette.responses import FileResponse
    return FileResponse(fpath, media_type="image/svg+xml")

@app.get("/favicon.ico", include_in_schema=False)
async def favicon_ico():
    import os
    fpath = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "favicon.svg")
    from starlette.responses import FileResponse
    return FileResponse(fpath, media_type="image/svg+xml")


from starlette.responses import PlainTextResponse as _PlainTextResponse

from apps.blog_posts import BLOG_POSTS, BLOG_POSTS_BY_SLUG, BLOG_CATEGORIES

import re as _re_mod
def _vacancy_slug(title: str | None, company: str | None = None) -> str:
    """Generate SEO-friendly slug from vacancy title and company."""
    parts = []
    if title:
        parts.append(title)
    if company:
        parts.append("at-" + company)
    raw = "-".join(parts).lower()
    raw = _re_mod.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return raw[:80] if raw else "vacancy"


@app.api_route("/robots.txt", methods=["GET", "HEAD"], response_class=_PlainTextResponse)
async def robots_txt():
    return _PlainTextResponse(
        "User-agent: *\n"
        "Allow: /\n"
        "Allow: /docs\n"
        "Allow: /about\n"
        "Allow: /landing\n"
        "Disallow: /api/\n"
        "Disallow: /settings\n"
        "Disallow: /login\n"
        "Disallow: /logout\n"
        "Disallow: /logs\n"
        "Disallow: /channels\n"
        "\n"
        "Sitemap: https://hirelens.xyz/sitemap.xml\n",
        media_type="text/plain"
    )

@app.api_route("/sitemap.xml", methods=["GET", "HEAD"], response_class=_PlainTextResponse)
async def sitemap_xml():
    static_urls = [
        ("/", "daily", "1.0"),
        ("/vacancies", "daily", "0.9"),
        ("/companies", "weekly", "0.8"),
        ("/analytics", "weekly", "0.7"),
        ("/about", "monthly", "0.6"),
        ("/blog", "weekly", "0.8"),
        ("/docs/welcome", "monthly", "0.6"),
    ]
    items = []
    for u, freq, pri in static_urls:
        items.append(
            f"  <url><loc>https://hirelens.xyz{u}</loc>"
            f"<changefreq>{freq}</changefreq><priority>{pri}</priority></url>"
        )
    # Add blog posts for SEO indexing
    for bp in BLOG_POSTS:
        d = bp.published_at.strftime("%Y-%m-%dT00:00:00Z")
        items.append(
            f"  <url><loc>https://hirelens.xyz/blog/{bp.slug}</loc>"
            f"<lastmod>{d}</lastmod><changefreq>monthly</changefreq><priority>0.7</priority></url>"
        )
    # Add recent vacancies for SEO indexing
    try:
      async with SessionLocal() as s:
        vac_rows = (await s.execute(
          select(Vacancy.id, Vacancy.title, Vacancy.company_name, Vacancy.created_at)
          .where(Vacancy.title.isnot(None))
          .order_by(Vacancy.created_at.desc())
          .limit(500)
        )).all()
        for vr in vac_rows:
          slug = _vacancy_slug(vr.title, vr.company_name)
          lastmod = f"<lastmod>{vr.created_at.strftime('%Y-%m-%dT%H:%M:%SZ')}</lastmod>" if vr.created_at else ""
          items.append(
            f"  <url><loc>https://hirelens.xyz/vacancies/{slug}-{vr.id}</loc>"
            f"{lastmod}<changefreq>weekly</changefreq><priority>0.6</priority></url>"
          )
    except Exception:
      pass
    # Add companies for SEO indexing
    try:
      async with SessionLocal() as s:
        comp_rows = (await s.execute(
          select(Company.id, Company.name, Company.updated_at)
          .where(Company.name.isnot(None))
          .order_by(Company.updated_at.desc().nullslast())
          .limit(500)
        )).all()
        for cr in comp_rows:
          slug = _vacancy_slug(cr.name)
          lastmod = f"<lastmod>{cr.updated_at.strftime('%Y-%m-%dT%H:%M:%SZ')}</lastmod>" if cr.updated_at else ""
          items.append(
            f"  <url><loc>https://hirelens.xyz/companies/{slug}-{cr.id}</loc>"
            f"{lastmod}<changefreq>weekly</changefreq><priority>0.5</priority></url>"
          )
    except Exception:
      pass
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(items)
        + "\n</urlset>"
    )
    return _PlainTextResponse(xml, media_type="application/xml")

@app.get("/", response_class=HTMLResponse)
async def root_page(request: Request):
  """Always serve landing page at root."""
  return await landing_page(request)


# ─── Blog routes ──────────────────────────────────────────────────────────────

@app.get("/blog", response_class=HTMLResponse)
async def blog_index(request: Request, category: str | None = None):
  """Public blog listing page."""
  posts = BLOG_POSTS
  if category:
    posts = [p for p in posts if p.category_slug == category]
  return templates.TemplateResponse(request, "blog.html", {
    "posts": posts,
    "categories": BLOG_CATEGORIES,
    "active_category": category,
  })


@app.get("/blog/{slug}", response_class=HTMLResponse)
async def blog_post(request: Request, slug: str):
  """Individual blog post page."""
  post = BLOG_POSTS_BY_SLUG.get(slug)
  if not post:
    raise HTTPException(status_code=404, detail="Article not found")
  related = [BLOG_POSTS_BY_SLUG[s] for s in post.related_slugs if s in BLOG_POSTS_BY_SLUG]
  return templates.TemplateResponse(request, "blog_post.html", {
    "post": post,
    "related": related,
  })


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
  async with SessionLocal() as s:
    total_vacancies = (await s.execute(select(func.count(Vacancy.id)))).scalar() or 0
    tg_channels = (await s.execute(select(func.count(Channel.id)))).scalar() or 0
    web_sources_cnt = 0
    try:
      web_sources_cnt = (await s.execute(select(func.count(WebSource.id)))).scalar() or 0
    except Exception:
      pass
    total_channels = tg_channels + web_sources_cnt
    total_companies = (await s.execute(select(func.count(Company.id)))).scalar() or 0

    domain_stats_rows = (await s.execute(text(
      """SELECT lower(btrim(d)) as category, count(*) as count
         FROM vacancies v CROSS JOIN LATERAL unnest(v.domains) as d
         WHERE d IS NOT NULL AND btrim(d) <> '' GROUP BY 1 ORDER BY count DESC"""
    ))).all()
    domain_stats = [(r.category, r.count) for r in domain_stats_rows]

    vacancies_24h = (await s.execute(select(func.count(Vacancy.id)).where(
      Vacancy.created_at >= func.now() - text("interval '24 hours'")
    ))).scalar() or 0

    vacancies_7d = (await s.execute(select(func.count(Vacancy.id)).where(
      Vacancy.created_at >= func.now() - text("interval '7 days'")
    ))).scalar() or 0

    avg_score = (await s.execute(select(func.round(func.avg(Vacancy.ai_score_value), 1)).where(
      Vacancy.ai_score_value.isnot(None)
    ))).scalar()

    with_salary = (await s.execute(select(func.count(Vacancy.id)).where(
      Vacancy.salary_min_usd.isnot(None) | Vacancy.salary_max_usd.isnot(None)
    ))).scalar() or 0
    salary_pct = round(with_salary / total_vacancies * 100) if total_vacancies else 0

    top_roles = (await s.execute(text(
      """SELECT role, count(*) as cnt FROM vacancies
         WHERE role IS NOT NULL AND role <> '' GROUP BY role ORDER BY cnt DESC LIMIT 8"""
    ))).all()

    top_companies = (await s.execute(text(
      """SELECT c.id, c.name, c.logo_url, c.industry, count(v.id) as cnt
         FROM companies c JOIN vacancies v ON v.company_id = c.id
         GROUP BY c.id ORDER BY cnt DESC LIMIT 6"""
    ))).all()

    last_vacancies = (await s.execute(
      select(Vacancy).order_by(desc(Vacancy.id)).limit(15)
    )).scalars().all()

  return templates.TemplateResponse(
    request, "dashboard.html",
    {
      "now": datetime.utcnow(),
      "total_vacancies": total_vacancies,
      "total_channels": total_channels,
      "total_companies": total_companies,
      "category_stats": domain_stats,
      "last_vacancies": last_vacancies,
      "vacancies_24h": vacancies_24h,
      "vacancies_7d": vacancies_7d,
      "avg_score": avg_score,
      "salary_pct": salary_pct,
      "top_roles": top_roles,
      "top_companies": top_companies,
    },
  )


@app.get("/vacancies", response_class=HTMLResponse)
async def vacancies_page(
  request: Request,
  page: int = Query(1, ge=1),
  per_page: int = Query(50, ge=1, le=200),
  category: str | None = Query(None),  # backward compat (mapped to domain)
  channel: str | None = Query(None),
  search: str | None = Query(None),
  search_mode: str = Query("auto"),
  domains: list[str] = Query([]),
  location_type: str | None = Query(None),
  # NOTE: must accept empty string values coming from HTML forms.
  # FastAPI parses query params before our code runs, so using `str` avoids int_parsing errors.
  salary_min_usd: str | None = Query(None),
  salary_max_usd: str | None = Query(None),
  risk_label: str | None = Query(None),
  seniority: str | None = Query(None),
  role: str | None = Query(None),
  employment_type: str | None = Query(None),
  score_min: str | None = Query(None),
  score_max: str | None = Query(None),
  sort_by: str = Query("date_desc"),
):
  async with SessionLocal() as s:
    vacancies: list[object]
    total: int

    domains_selected = [str(d).strip().lower() for d in domains if str(d).strip()]
    if not domains_selected and category:
      domains_selected = [str(category).strip().lower()]

    salary_min_usd_val: int | None = None
    salary_max_usd_val: int | None = None
    try:
      if salary_min_usd is not None and str(salary_min_usd).strip() != "":
        salary_min_usd_val = int(str(salary_min_usd).strip())
    except Exception:
      salary_min_usd_val = None
    try:
      if salary_max_usd is not None and str(salary_max_usd).strip() != "":
        salary_max_usd_val = int(str(salary_max_usd).strip())
    except Exception:
      salary_max_usd_val = None

    score_min_val: int | None = None
    score_max_val: int | None = None
    try:
      if score_min is not None and str(score_min).strip() != "":
        score_min_val = int(str(score_min).strip())
    except Exception:
      score_min_val = None
    try:
      if score_max is not None and str(score_max).strip() != "":
        score_max_val = int(str(score_max).strip())
    except Exception:
      score_max_val = None

    seniority_val = (seniority or "").strip().lower() or None
    role_val = (role or "").strip() or None
    employment_type_val = (employment_type or "").strip().lower() or None

    effective_mode = search_mode
    if effective_mode == "auto" and search and search.strip():
      words = search.strip().split()
      effective_mode = "semantic" if len(words) >= 4 else "text"

    if effective_mode == "semantic" and search and search.strip():
      embedding = await embed_text(search)
      if not embedding:
        raise HTTPException(status_code=503, detail="Embeddings are not available (configure OPENROUTER_API_KEY).")

      vec = "[" + ",".join(f"{x:.6f}" for x in embedding) + "]"
      offset = (page - 1) * per_page

      where = ["v.embedding IS NOT NULL"]
      params: dict = {"vec": vec, "limit": per_page, "offset": offset}

      if domains_selected:
        domains_arr = "{" + ",".join(f'"{d}"' for d in domains_selected) + "}"
        where.append("v.domains && (:domains_arr)::text[]")
        params["domains_arr"] = domains_arr

      if location_type:
        where.append("v.location_type = :location_type")
        params["location_type"] = str(location_type).strip().lower()

      if risk_label:
        rl = str(risk_label).strip()
        if rl == "not-high-risk":
          where.append("(v.risk_label IS NULL OR v.risk_label != 'high-risk')")
        else:
          where.append("v.risk_label = :risk_label")
          params["risk_label"] = rl

      if salary_min_usd_val is not None:
        where.append("v.salary_max_usd IS NOT NULL AND v.salary_max_usd >= :salary_min_usd")
        params["salary_min_usd"] = salary_min_usd_val
      if salary_max_usd_val is not None:
        where.append("v.salary_min_usd IS NOT NULL AND v.salary_min_usd <= :salary_max_usd")
        params["salary_max_usd"] = salary_max_usd_val

      if channel:
        where.append("v.tg_channel_username = :channel")
        params["channel"] = channel

      if seniority_val:
        where.append("lower(v.seniority) = :seniority")
        params["seniority"] = seniority_val
      if role_val:
        where.append("v.role = :role")
        params["role"] = role_val
      if employment_type_val:
        where.append("v.metadata->>'employment_type' = :employment_type")
        params["employment_type"] = employment_type_val
      if score_min_val is not None:
        where.append("v.ai_score_value >= :score_min")
        params["score_min"] = score_min_val
      if score_max_val is not None:
        where.append("v.ai_score_value <= :score_max")
        params["score_max"] = score_max_val

      where_sql = " AND ".join(where)
      sql_count = f"SELECT count(*) FROM vacancies v WHERE {where_sql}"

      _sort_map_sql = {
        "date_desc": "v.id DESC",
        "date_asc": "v.id ASC",
        "salary_desc": "COALESCE(v.salary_max_usd, 0) DESC, v.id DESC",
        "salary_asc": "COALESCE(v.salary_min_usd, 999999999) ASC, v.id DESC",
        "score_desc": "COALESCE(v.ai_score_value, 0) DESC, v.id DESC",
        "score_asc": "COALESCE(v.ai_score_value, 0) ASC, v.id DESC",
        "relevance": "v.embedding <=> (:vec)::vector ASC",
      }
      order_clause = _sort_map_sql.get(sort_by, _sort_map_sql.get("relevance", "v.id DESC"))
      if sort_by == "date_desc" and effective_mode == "semantic":
        order_clause = _sort_map_sql["relevance"]

      sql_select = f"""
        SELECT
          v.id,
          v.title,
          v.company_name,
          v.role,
          v.domains,
          v.risk_label,
          v.ai_score_value,
          v.location_type,
          v.salary_min_usd,
          v.salary_max_usd,
          v.tg_channel_username,
          v.raw_text,
          v.summary_en,
          v.category,
          v.stack,
          v.created_at,
          v.seniority,
          v.english_level,
          v.metadata_json,
          c.logo_url AS company_logo_url
        FROM vacancies v
        LEFT JOIN companies c ON c.id = v.company_id
        WHERE {where_sql}
        ORDER BY {order_clause}
        LIMIT :limit OFFSET :offset
      """

      total = (await s.execute(text(sql_count), params)).scalar() or 0
      rows = (await s.execute(text(sql_select), params)).mappings().all()
      vacancies = [dict(r) for r in rows]
    else:
      query = select(Vacancy)

      if domains_selected:
        domain_conds = [Vacancy.domains.contains([d]) for d in domains_selected]
        query = query.where(or_(*domain_conds))

      if location_type:
        query = query.where(Vacancy.location_type == str(location_type).strip().lower())

      if risk_label:
        rl = str(risk_label).strip()
        if rl == "not-high-risk":
          query = query.where(or_(Vacancy.risk_label.is_(None), Vacancy.risk_label != "high-risk"))
        else:
          query = query.where(Vacancy.risk_label == rl)

      if salary_min_usd_val is not None:
        try:
          mi = int(salary_min_usd_val)
          query = query.where(Vacancy.salary_max_usd.isnot(None)).where(Vacancy.salary_max_usd >= mi)
        except Exception:
          pass
      if salary_max_usd_val is not None:
        try:
          ma = int(salary_max_usd_val)
          query = query.where(Vacancy.salary_min_usd.isnot(None)).where(Vacancy.salary_min_usd <= ma)
        except Exception:
          pass

      if seniority_val:
        query = query.where(func.lower(Vacancy.seniority) == seniority_val)
      if role_val:
        query = query.where(Vacancy.role == role_val)
      if employment_type_val:
        query = query.where(Vacancy.metadata_json["employment_type"].as_string() == employment_type_val)
      if score_min_val is not None:
        query = query.where(Vacancy.ai_score_value >= score_min_val)
      if score_max_val is not None:
        query = query.where(Vacancy.ai_score_value <= score_max_val)

      if channel:
        query = query.where(Vacancy.tg_channel_username == channel)
      if search and search.strip():
        search_filter = or_(
          Vacancy.title.ilike(f"%{search}%"),
          Vacancy.company_name.ilike(f"%{search}%"),
          Vacancy.raw_text.ilike(f"%{search}%"),
        )
        query = query.where(search_filter)

      total = (await s.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0

      _sort_orm = {
        "date_desc": [desc(Vacancy.id)],
        "date_asc": [Vacancy.id],
        "salary_desc": [desc(func.coalesce(Vacancy.salary_max_usd, 0)), desc(Vacancy.id)],
        "salary_asc": [func.coalesce(Vacancy.salary_min_usd, 999999999), desc(Vacancy.id)],
        "score_desc": [desc(func.coalesce(Vacancy.ai_score_value, 0)), desc(Vacancy.id)],
        "score_asc": [func.coalesce(Vacancy.ai_score_value, 0), desc(Vacancy.id)],
      }
      order_cols = _sort_orm.get(sort_by, _sort_orm["date_desc"])

      rows_orm = (
        await s.execute(
          select(Vacancy, Company.logo_url)
          .select_from(Vacancy)
          .outerjoin(Company, Company.id == Vacancy.company_id)
          .where(Vacancy.id.in_(
            query.with_only_columns(Vacancy.id)
            .order_by(*order_cols)
            .offset((page - 1) * per_page)
            .limit(per_page)
          ))
          .order_by(*order_cols)
        )
      ).all()
      vacancies = []
      for vac_obj, logo in rows_orm:
        vac_obj.company_logo_url = logo
        vacancies.append(vac_obj)
    
    domain_options_rows = (
      await s.execute(
        text(
          """
          SELECT lower(btrim(d)) as domain, count(*) as count
          FROM vacancies v
          CROSS JOIN LATERAL unnest(v.domains) as d
          WHERE d IS NOT NULL AND btrim(d) <> ''
          GROUP BY 1
          ORDER BY count DESC
          """
        )
      )
    ).all()
    domain_options = [(r.domain, r.count) for r in domain_options_rows]
    # UI: show stable domain set even if some domains have 0 vacancies.
    known_domains = ["web3", "crypto", "defi", "nft", "dao", "gamefi", "rwa", "l1l2", "ai", "dev", "design", "igaming", "gaming", "traffic", "fintech", "marketing", "hr", "analytics", "product", "support"]
    existing_map = {d: int(c) for d, c in domain_options}
    domain_options = [(d, existing_map.get(d, 0)) for d in known_domains]

    # Channel options (for filter bar)
    
    channels = (
      await s.execute(
        select(Vacancy.tg_channel_username, func.count(Vacancy.id).label("count"))
        .where(Vacancy.tg_channel_username.isnot(None))
        .group_by(Vacancy.tg_channel_username)
        .order_by(desc("count"))
      )
    ).all()

  return templates.TemplateResponse(
    request, "vacancies.html",
    {
      "vacancies": vacancies,
      "total": total,
      "page": page,
      "per_page": per_page,
      "total_pages": (total + per_page - 1) // per_page if total > 0 else 1,
      "domains_selected": domains_selected,
      "location_type": location_type,
      "salary_min_usd": salary_min_usd_val,
      "salary_max_usd": salary_max_usd_val,
      "risk_label": risk_label,
      "channel": channel,
      "search": search,
      "search_mode": search_mode,
      "domain_options": domain_options,
      "channels": channels,
      "seniority": seniority_val,
      "role": role_val,
      "employment_type": employment_type_val,
      "score_min": score_min_val,
      "score_max": score_max_val,
      "sort_by": sort_by,
      "is_admin": _is_authenticated(request),
    },
  )


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(
  request: Request,
  _: bool = Depends(require_auth),
  limit: int = Query(200, ge=10, le=1000),
):
  """
  Human-friendly parser logs (English) for admin.
  """
  async with SessionLocal() as s:
    # Backfill: if userbot was running an older code version (or migrations
    # were not applied), the UI may show "No parser logs yet.".
    # Here we ensure at least `vacancy_added` events exist for recent vacancies.
    recent_vacancies = (
      await s.execute(select(Vacancy).order_by(desc(Vacancy.id)).limit(50))
    ).scalars().all()
    recent_ids = [v.id for v in recent_vacancies if v.id is not None]
    if recent_ids:
      existing_rows = (
        await s.execute(
          select(ParserLog.vacancy_id).where(
            ParserLog.event == "vacancy_added",
            ParserLog.vacancy_id.in_(recent_ids),
          )
        )
      ).scalars().all()
      existing_set = {int(x) for x in existing_rows if x is not None}

      missing = [v for v in recent_vacancies if int(v.id) not in existing_set]
      for v in missing:
        s.add(
          ParserLog(
            level="INFO",
            event="vacancy_added",
            message_en=f"Vacancy added. ID {v.id}",
            channel_username=v.tg_channel_username,
            tg_message_id=v.tg_message_id,
            vacancy_id=v.id,
            extra={},
            created_at=v.created_at,
          )
        )
      if missing:
        await s.commit()

    rows = (
      await s.execute(
        select(ParserLog)
        .order_by(desc(ParserLog.id))
        .limit(limit)
      )
    ).scalars().all()

  # Reverse for chronological order.
  rows = list(reversed(rows))

  # Resolve channel_id from username so logs can link into admin UI.
  usernames = {r.channel_username for r in rows if getattr(r, "channel_username", None)}
  channel_id_by_username: dict[str, int] = {}
  if usernames:
    channel_rows = (
      await s.execute(
        select(Channel).where(Channel.username.in_(list(usernames)))
      )
    ).scalars().all()
    channel_id_by_username = {c.username: int(c.id) for c in channel_rows if c.username}

  logs = [
    {
      "id": r.id,
      "created_at": r.created_at.isoformat() if r.created_at else None,
      "level": r.level,
      "event": r.event,
      "message_en": r.message_en,
      "channel_username": r.channel_username,
      "channel_id": channel_id_by_username.get(r.channel_username) if r.channel_username else None,
      "tg_message_id": r.tg_message_id,
      "vacancy_id": r.vacancy_id,
      "extra": r.extra or {},
    }
    for r in rows
  ]

  return templates.TemplateResponse(
    request, "logs.html",
    {
      "logs": logs,
      "limit": limit,
    },
  )


async def _load_api_keys_overview(s):
  keys = (await s.execute(select(ApiKey).order_by(desc(ApiKey.id)))).scalars().all()
  day_start = datetime.utcnow() - timedelta(days=1)

  used_rows = (
    await s.execute(
      select(
        ApiKeyUsage.api_key_id,
        func.count(ApiKeyUsage.id).label("used_24h"),
        func.max(ApiKeyUsage.requested_at).label("last_used_at"),
      )
      .where(ApiKeyUsage.requested_at >= day_start)
      .group_by(ApiKeyUsage.api_key_id)
    )
  ).all()

  usage_map: dict[int, dict] = {}
  for r in used_rows:
    usage_map[int(r.api_key_id)] = {
      "used_24h": int(r.used_24h or 0),
      "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
    }

  return keys, usage_map


@app.get("/api/keys", response_class=HTMLResponse)
async def api_keys_page(
  request: Request,
  _: bool = Depends(require_auth),
):
  async with SessionLocal() as s:
    keys, usage_map = await _load_api_keys_overview(s)
    admin_users = (
      await s.execute(select(AdminUser).where(AdminUser.is_active == True).order_by(AdminUser.id.asc()))
    ).scalars().all()
    owner_email_map = {u.id: u.email for u in admin_users}
    now = datetime.utcnow()
    expires_period_map: dict[int, str] = {}
    for k in keys:
      if getattr(k, "expires_at", None) is None:
        expires_period_map[int(k.id)] = "forever"
        continue
      try:
        created_at = getattr(k, "created_at", None) or now
        delta_days = (k.expires_at - created_at).total_seconds() / 86400.0
        if delta_days <= 45:
          expires_period_map[int(k.id)] = "1m"
        elif delta_days <= 120:
          expires_period_map[int(k.id)] = "3m"
        else:
          expires_period_map[int(k.id)] = "1y"
      except Exception:
        expires_period_map[int(k.id)] = "custom"
  return templates.TemplateResponse(
    request, "api_keys.html",
    {
      "keys": keys,
      "usage_map": usage_map,
      "admin_users": admin_users,
      "owner_email_map": owner_email_map,
      "expires_period_map": expires_period_map,
      "generated_token": None,
      "flash": request.query_params.get("flash"),
      "created": request.query_params.get("created"),
    },
  )


@app.get("/api/docs", response_class=HTMLResponse)
async def api_docs_page(
  request: Request,
  _: bool = Depends(require_auth),
):
  return templates.TemplateResponse(request, "api_docs.html")


@app.post("/api/keys/create", response_class=HTMLResponse)
async def api_keys_create(
  request: Request,
  _: bool = Depends(require_auth),
):
  form = await request.form()
  current_admin_id_raw = request.session.get("user_id")
  try:
    current_admin_id = int(current_admin_id_raw) if current_admin_id_raw is not None else None
  except Exception:
    current_admin_id = None

  name = (form.get("name") or "").strip()
  if not name:
    raise HTTPException(status_code=400, detail="name is required")

  domains = _parse_csv_list(form.get("domains"))
  role = (form.get("role") or "").strip() or None
  recruiter = (form.get("recruiter") or "").strip() or None
  company_domain = (form.get("company_domain") or "").strip().lower() or None
  location_type = (form.get("location_type") or "").strip() or None
  risk_label = (form.get("risk_label") or "").strip() or None

  salary_min_raw = (form.get("salary_min_usd") or "").strip()
  salary_max_raw = (form.get("salary_max_usd") or "").strip()
  salary_min_usd = int(salary_min_raw) if salary_min_raw else None
  salary_max_usd = int(salary_max_raw) if salary_max_raw else None

  requests_per_minute_raw = (form.get("requests_per_minute") or "").strip()
  daily_quota_raw = (form.get("daily_quota") or "").strip()
  limits: dict = {}
  if requests_per_minute_raw:
    limits["requests_per_minute"] = int(requests_per_minute_raw)
  if daily_quota_raw:
    limits["daily_quota"] = int(daily_quota_raw)

  is_active_raw = (form.get("is_active") or "").lower()
  is_active = is_active_raw in ("1", "true", "yes", "on") or not is_active_raw

  owner_id_raw = (form.get("owner_id") or "").strip()
  owner_id = None
  if owner_id_raw:
    try:
      owner_id = int(owner_id_raw)
    except Exception:
      owner_id = None
  if owner_id is None:
    owner_id = current_admin_id

  expires_period = (form.get("expires_period") or "forever").strip()
  expires_at = None
  if expires_period in ("1m", "3m", "1y"):
    now = datetime.utcnow()
    days = 30 if expires_period == "1m" else (90 if expires_period == "3m" else 365)
    expires_at = now + timedelta(days=days)

  token = _generate_api_key_token()
  key = ApiKey(
    name=name,
    api_key_hash=_hash_api_key_token(token),
    is_active=is_active,
    owner_id=owner_id,
    expires_at=expires_at,
    config={
      "filters": {
        "domains": domains,
        "role": role,
        "recruiter": recruiter,
        "company_domain": company_domain,
        "location_type": location_type,
        "salary_min_usd": salary_min_usd,
        "salary_max_usd": salary_max_usd,
        "risk_label": risk_label,
      }
    },
    limits=limits,
  )

  async with SessionLocal() as s:
    s.add(key)
    await s.commit()
    await s.refresh(key)
    keys, usage_map = await _load_api_keys_overview(s)
    admin_users = (
      await s.execute(select(AdminUser).where(AdminUser.is_active == True).order_by(AdminUser.id.asc()))
    ).scalars().all()
    owner_email_map = {u.id: u.email for u in admin_users}
    now = datetime.utcnow()
    expires_period_map: dict[int, str] = {}
    for k in keys:
      if getattr(k, "expires_at", None) is None:
        expires_period_map[int(k.id)] = "forever"
        continue
      try:
        created_at = getattr(k, "created_at", None) or now
        delta_days = (k.expires_at - created_at).total_seconds() / 86400.0
        if delta_days <= 45:
          expires_period_map[int(k.id)] = "1m"
        elif delta_days <= 120:
          expires_period_map[int(k.id)] = "3m"
        else:
          expires_period_map[int(k.id)] = "1y"
      except Exception:
        expires_period_map[int(k.id)] = "custom"

  return templates.TemplateResponse(
    request, "api_keys.html",
    {
      "keys": keys,
      "usage_map": usage_map,
      "admin_users": admin_users,
      "owner_email_map": owner_email_map,
      "expires_period_map": expires_period_map,
      "generated_token": token,
      "flash": f"API key created (id={key.id})",
      "created": "1",
    },
  )


@app.post("/api/keys/{api_key_id}/toggle", response_class=HTMLResponse)
async def api_keys_toggle(
  request: Request,
  api_key_id: int,
  _: bool = Depends(require_auth),
):
  async with SessionLocal() as s:
    key = (await s.execute(select(ApiKey).where(ApiKey.id == api_key_id))).scalar_one_or_none()
    if not key:
      raise HTTPException(status_code=404, detail="API key not found")
    key.is_active = not bool(key.is_active)
    await s.commit()
  return RedirectResponse(url="/api/keys?flash=updated", status_code=303)


@app.post("/api/keys/{api_key_id}/delete", response_class=HTMLResponse)
async def api_keys_delete(
  request: Request,
  api_key_id: int,
  _: bool = Depends(require_auth),
):
  async with SessionLocal() as s:
    await s.execute(delete(ApiKey).where(ApiKey.id == api_key_id))
    await s.commit()
  return RedirectResponse(url="/api/keys?flash=deleted", status_code=303)


@app.post("/api/keys/{api_key_id}/regenerate", response_class=HTMLResponse)
async def api_keys_regenerate(
  request: Request,
  api_key_id: int,
  _: bool = Depends(require_auth),
):
  token = _generate_api_key_token()
  async with SessionLocal() as s:
    key = (await s.execute(select(ApiKey).where(ApiKey.id == api_key_id))).scalar_one_or_none()
    if not key:
      raise HTTPException(status_code=404, detail="API key not found")
    key.api_key_hash = _hash_api_key_token(token)
    await s.commit()
    keys, usage_map = await _load_api_keys_overview(s)
    admin_users = (
      await s.execute(select(AdminUser).where(AdminUser.is_active == True).order_by(AdminUser.id.asc()))
    ).scalars().all()
    owner_email_map = {u.id: u.email for u in admin_users}
    now = datetime.utcnow()
    expires_period_map: dict[int, str] = {}
    for k in keys:
      if getattr(k, "expires_at", None) is None:
        expires_period_map[int(k.id)] = "forever"
        continue
      try:
        created_at = getattr(k, "created_at", None) or now
        delta_days = (k.expires_at - created_at).total_seconds() / 86400.0
        if delta_days <= 45:
          expires_period_map[int(k.id)] = "1m"
        elif delta_days <= 120:
          expires_period_map[int(k.id)] = "3m"
        else:
          expires_period_map[int(k.id)] = "1y"
      except Exception:
        expires_period_map[int(k.id)] = "custom"

  return templates.TemplateResponse(
    request, "api_keys.html",
    {
      "keys": keys,
      "usage_map": usage_map,
      "admin_users": admin_users,
      "owner_email_map": owner_email_map,
      "expires_period_map": expires_period_map,
      "generated_token": token,
      "flash": f"API key regenerated (id={api_key_id})",
      "created": "0",
    },
  )


@app.post("/api/keys/{api_key_id}/update", response_class=HTMLResponse)
async def api_keys_update(
  request: Request,
  api_key_id: int,
  _: bool = Depends(require_auth),
):
  form = await request.form()
  current_admin_id_raw = request.session.get("user_id")
  try:
    current_admin_id = int(current_admin_id_raw) if current_admin_id_raw is not None else None
  except Exception:
    current_admin_id = None

  name = (form.get("name") or "").strip()
  domains = _parse_csv_list(form.get("domains"))
  role = (form.get("role") or "").strip() or None
  recruiter = (form.get("recruiter") or "").strip() or None
  company_domain = (form.get("company_domain") or "").strip().lower() or None
  location_type = (form.get("location_type") or "").strip() or None
  risk_label = (form.get("risk_label") or "").strip() or None

  salary_min_raw = (form.get("salary_min_usd") or "").strip()
  salary_max_raw = (form.get("salary_max_usd") or "").strip()
  salary_min_usd = int(salary_min_raw) if salary_min_raw else None
  salary_max_usd = int(salary_max_raw) if salary_max_raw else None

  requests_per_minute_raw = (form.get("requests_per_minute") or "").strip()
  daily_quota_raw = (form.get("daily_quota") or "").strip()
  limits: dict = {}
  if requests_per_minute_raw:
    limits["requests_per_minute"] = int(requests_per_minute_raw)
  if daily_quota_raw:
    limits["daily_quota"] = int(daily_quota_raw)

  is_active_raw = (form.get("is_active") or "").lower()
  is_active = is_active_raw in ("1", "true", "yes", "on")

  owner_id_raw = (form.get("owner_id") or "").strip()
  owner_id: int | None = None
  set_owner_id = False
  if owner_id_raw:
    try:
      owner_id = int(owner_id_raw)
      set_owner_id = True
    except Exception:
      owner_id = None

  expires_period = (form.get("expires_period") or "forever").strip()
  expires_at = None
  keep_existing_expires = expires_period == "custom"
  if expires_period in ("1m", "3m", "1y"):
    now = datetime.utcnow()
    days = 30 if expires_period == "1m" else (90 if expires_period == "3m" else 365)
    expires_at = now + timedelta(days=days)

  async with SessionLocal() as s:
    key = (await s.execute(select(ApiKey).where(ApiKey.id == api_key_id))).scalar_one_or_none()
    if not key:
      raise HTTPException(status_code=404, detail="API key not found")

    if name:
      key.name = name
    key.is_active = is_active
    if set_owner_id:
      key.owner_id = owner_id
    key.expires_at = key.expires_at if keep_existing_expires else expires_at
    key.config = {
      "filters": {
        "domains": domains,
        "role": role,
        "recruiter": recruiter,
        "company_domain": company_domain,
        "location_type": location_type,
        "salary_min_usd": salary_min_usd,
        "salary_max_usd": salary_max_usd,
        "risk_label": risk_label,
      }
    }
    key.limits = limits

    await s.commit()
    keys, usage_map = await _load_api_keys_overview(s)
    admin_users = (
      await s.execute(select(AdminUser).where(AdminUser.is_active == True).order_by(AdminUser.id.asc()))
    ).scalars().all()
    owner_email_map = {u.id: u.email for u in admin_users}
    now = datetime.utcnow()
    expires_period_map: dict[int, str] = {}
    for k in keys:
      if getattr(k, "expires_at", None) is None:
        expires_period_map[int(k.id)] = "forever"
        continue
      try:
        created_at = getattr(k, "created_at", None) or now
        delta_days = (k.expires_at - created_at).total_seconds() / 86400.0
        if delta_days <= 45:
          expires_period_map[int(k.id)] = "1m"
        elif delta_days <= 120:
          expires_period_map[int(k.id)] = "3m"
        else:
          expires_period_map[int(k.id)] = "1y"
      except Exception:
        expires_period_map[int(k.id)] = "custom"

  return templates.TemplateResponse(
    request, "api_keys.html",
    {
      "keys": keys,
      "usage_map": usage_map,
      "admin_users": admin_users,
      "owner_email_map": owner_email_map,
      "expires_period_map": expires_period_map,
      "generated_token": None,
      "flash": "API key updated",
      "created": "0",
    },
  )


@app.get("/api/public/landing")
async def api_public_landing(request: Request):
  """Return data for the landing page: stats, companies with logos, recent job titles."""
  api_key_token = (request.headers.get("X-API-Key") or "").strip()
  if not api_key_token:
    raise HTTPException(status_code=401, detail="Missing X-API-Key header")

  token_hash = _hash_api_key_token(api_key_token)
  now = datetime.utcnow()
  async with SessionLocal() as s:
    api_key = (
      await s.execute(
        select(ApiKey).where(
          ApiKey.api_key_hash == token_hash,
          ApiKey.is_active == True,  # noqa: E712
          or_(ApiKey.expires_at.is_(None), ApiKey.expires_at > now),
        )
      )
    ).scalar_one_or_none()
    if not api_key:
      raise HTTPException(status_code=401, detail="Invalid API key")

    await _enforce_api_key_limits_and_log(
      s=s, api_key=api_key, endpoint="/api/public/landing", status_code=200,
    )

    total_vacancies = (await s.execute(text("SELECT count(*) FROM vacancies"))).scalar() or 0

    companies_rows = (await s.execute(text(
      """SELECT c.name, c.logo_url, c.industry, count(v.id) AS job_count
         FROM companies c
         JOIN vacancies v ON v.company_id = c.id
         WHERE c.logo_url IS NOT NULL AND c.logo_url != ''
         GROUP BY c.id, c.name, c.logo_url, c.industry
         ORDER BY job_count DESC
         LIMIT 40"""
    ))).mappings().all()

    total_companies = (await s.execute(text(
      "SELECT count(DISTINCT company_name) FROM vacancies WHERE company_name IS NOT NULL AND company_name != ''"
    ))).scalar() or 0

    recent_jobs = (await s.execute(text(
      """SELECT v.title, v.company_name, v.salary_min_usd, v.salary_max_usd, v.location_type, v.ai_score_value, v.id
         FROM vacancies v
         ORDER BY v.created_at DESC
         LIMIT 20"""
    ))).mappings().all()

    return {
      "stats": {
        "total_vacancies": total_vacancies,
        "total_companies": total_companies,
      },
      "companies": [
        {"name": r["name"], "logo_url": r["logo_url"], "industry": r["industry"], "job_count": r["job_count"]}
        for r in companies_rows
      ],
      "recent_jobs": [
        {
          "id": r["id"],
          "title": r["title"],
          "company_name": r["company_name"],
          "salary_min": r["salary_min_usd"],
          "salary_max": r["salary_max_usd"],
          "location_type": r["location_type"],
          "ai_score": r["ai_score_value"],
        }
        for r in recent_jobs
      ],
    }


@app.get("/api/public/facets")
async def api_public_facets(request: Request):
  """Return distinct filter values: skills, roles, countries, seniority, domains."""
  api_key_token = (request.headers.get("X-API-Key") or "").strip()
  if not api_key_token:
    raise HTTPException(status_code=401, detail="Missing X-API-Key header")

  token_hash = _hash_api_key_token(api_key_token)
  now = datetime.utcnow()
  async with SessionLocal() as s:
    api_key = (
      await s.execute(
        select(ApiKey).where(
          ApiKey.api_key_hash == token_hash,
          ApiKey.is_active == True,  # noqa: E712
          or_(ApiKey.expires_at.is_(None), ApiKey.expires_at > now),
        )
      )
    ).scalar_one_or_none()
    if not api_key:
      raise HTTPException(status_code=401, detail="Invalid API key")

    await _enforce_api_key_limits_and_log(
      s=s, api_key=api_key, endpoint="/api/public/facets", status_code=200,
    )

    skills_rows = (await s.execute(text(
      "SELECT skill, count(*) AS cnt FROM (SELECT unnest(stack) AS skill FROM vacancies) t WHERE skill IS NOT NULL AND skill != '' GROUP BY skill ORDER BY cnt DESC LIMIT 100"
    ))).mappings().all()

    roles_rows = (await s.execute(text(
      "SELECT role, count(*) AS cnt FROM vacancies WHERE role IS NOT NULL AND role != '' GROUP BY role ORDER BY cnt DESC LIMIT 100"
    ))).mappings().all()

    countries_rows = (await s.execute(text(
      "SELECT country_city, count(*) AS cnt FROM vacancies WHERE country_city IS NOT NULL AND country_city != '' GROUP BY country_city ORDER BY cnt DESC LIMIT 100"
    ))).mappings().all()

    seniority_rows = (await s.execute(text(
      "SELECT seniority, count(*) AS cnt FROM vacancies WHERE seniority IS NOT NULL AND seniority != '' GROUP BY seniority ORDER BY cnt DESC"
    ))).mappings().all()

    domains_rows = (await s.execute(text(
      "SELECT domain, count(*) AS cnt FROM (SELECT unnest(domains) AS domain FROM vacancies) t WHERE domain IS NOT NULL AND domain != '' GROUP BY domain ORDER BY cnt DESC LIMIT 50"
    ))).mappings().all()

    return {
      "skills": [{"name": r["skill"], "count": r["cnt"]} for r in skills_rows],
      "roles": [{"name": r["role"], "count": r["cnt"]} for r in roles_rows],
      "countries": [{"name": r["country_city"], "count": r["cnt"]} for r in countries_rows],
      "seniority": [{"name": r["seniority"], "count": r["cnt"]} for r in seniority_rows],
      "domains": [{"name": r["domain"], "count": r["cnt"]} for r in domains_rows],
    }


@app.get("/api/public/vacancies")
async def api_public_vacancies(
  request: Request,
  page: int = Query(1, ge=1),
  per_page: int = Query(50, ge=1, le=200),
  search: str | None = Query(None),
  domains: list[str] = Query([]),
  location_type: str | None = Query(None),
  seniority: str | None = Query(None),
  role: str | None = Query(None, description="Canonical role filter, e.g. 'Backend Developer'"),
  employment_type: str | None = Query(None),
  salary_min_usd: str | None = Query(None),
  salary_max_usd: str | None = Query(None),
  score_min: str | None = Query(None),
  score_max: str | None = Query(None),
  risk_label: str | None = Query(None),
  company_name: str | None = Query(None),
  sort: str | None = Query(None, description="Sort order: date_asc, date_desc (default), salary_asc, salary_desc, score_asc, score_desc"),
):
  api_key_token = (request.headers.get("X-API-Key") or "").strip()
  if not api_key_token:
    raise HTTPException(status_code=401, detail="Missing X-API-Key header")

  token_hash = _hash_api_key_token(api_key_token)
  now = datetime.utcnow()
  async with SessionLocal() as s:
    api_key = (
      await s.execute(
        select(ApiKey).where(
          ApiKey.api_key_hash == token_hash,
          ApiKey.is_active == True,  # noqa: E712
          or_(ApiKey.expires_at.is_(None), ApiKey.expires_at > now),
        )
      )
    ).scalar_one_or_none()
    if not api_key:
      raise HTTPException(status_code=401, detail="Invalid API key")

    await _enforce_api_key_limits_and_log(
      s=s, api_key=api_key, endpoint="/api/public/vacancies", status_code=200,
    )

    cfg = api_key.config or {}
    key_filters = cfg.get("filters") or {}
    out_lang = (cfg.get("output") or {}).get("language") or "en"

    query = select(Vacancy)

    # Domains: query param > key config
    q_domains = [str(d).strip().lower() for d in domains if str(d).strip()]
    if not q_domains:
      q_domains = [str(d).strip().lower() for d in (key_filters.get("domains") or []) if str(d).strip()]
    if q_domains:
      domain_conds = [Vacancy.domains.contains([d]) for d in q_domains]
      query = query.where(or_(*domain_conds))

    q_location = (location_type or "").strip().lower() or (key_filters.get("location_type") or "").strip().lower() or None
    if q_location:
      query = query.where(Vacancy.location_type == q_location)

    q_risk = (risk_label or "").strip() or (key_filters.get("risk_label") or "").strip() or None
    if q_risk:
      if q_risk == "not-high-risk":
        query = query.where(or_(Vacancy.risk_label.is_(None), Vacancy.risk_label != "high-risk"))
      else:
        query = query.where(Vacancy.risk_label == q_risk)

    q_seniority = (seniority or "").strip().lower() or None
    if q_seniority:
      query = query.where(func.lower(Vacancy.seniority) == q_seniority)

    q_role = (role or "").strip() or None
    if q_role:
      query = query.where(Vacancy.role == q_role)

    q_employment = (employment_type or "").strip().lower() or None
    if q_employment:
      query = query.where(Vacancy.metadata_json["employment_type"].as_string() == q_employment)

    if company_name and company_name.strip():
      query = query.where(Vacancy.company_name.ilike(f"%{company_name.strip()}%"))

    _sal_min = _safe_int(salary_min_usd) if salary_min_usd else _safe_int(key_filters.get("salary_min_usd"))
    _sal_max = _safe_int(salary_max_usd) if salary_max_usd else _safe_int(key_filters.get("salary_max_usd"))
    if _sal_min is not None:
      query = query.where(Vacancy.salary_max_usd.isnot(None)).where(Vacancy.salary_max_usd >= _sal_min)
    if _sal_max is not None:
      query = query.where(Vacancy.salary_min_usd.isnot(None)).where(Vacancy.salary_min_usd <= _sal_max)

    _sc_min = _safe_int(score_min)
    _sc_max = _safe_int(score_max)
    if _sc_min is not None:
      query = query.where(Vacancy.ai_score_value >= _sc_min)
    if _sc_max is not None:
      query = query.where(Vacancy.ai_score_value <= _sc_max)

    if search and search.strip():
      query = query.where(or_(
        Vacancy.title.ilike(f"%{search.strip()}%"),
        Vacancy.company_name.ilike(f"%{search.strip()}%"),
        Vacancy.raw_text.ilike(f"%{search.strip()}%"),
      ))

    # Key-level role/recruiter filters
    role_f = key_filters.get("role")
    if role_f:
      query = query.where(Vacancy.role.ilike(f"%{str(role_f).strip()}%"))
    recruiter_f = key_filters.get("recruiter")
    if recruiter_f:
      query = query.where(Vacancy.recruiter.ilike(f"%{str(recruiter_f).strip()}%"))

    total = (await s.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0

    _sort_map = {
      "date_asc": Vacancy.created_at.asc(),
      "date_desc": Vacancy.created_at.desc(),
      "salary_asc": Vacancy.salary_min_usd.asc().nulls_last(),
      "salary_desc": Vacancy.salary_max_usd.desc().nulls_last(),
      "score_asc": Vacancy.ai_score_value.asc().nulls_last(),
      "score_desc": Vacancy.ai_score_value.desc().nulls_last(),
    }
    order_clause = _sort_map.get((sort or "").strip().lower(), desc(Vacancy.id))

    rows_orm = (
      await s.execute(
        select(Vacancy, Company.logo_url, Company.website.label("company_website_enriched"),
               Company.industry, Company.size.label("company_size_enriched"),
               Company.founded, Company.headquarters, Company.summary.label("company_summary"),
               Company.socials.label("company_socials"), Company.domains.label("company_domains"))
        .select_from(Vacancy)
        .outerjoin(Company, Company.id == Vacancy.company_id)
        .where(Vacancy.id.in_(
          query.with_only_columns(Vacancy.id)
          .order_by(order_clause)
          .offset((page - 1) * per_page)
          .limit(per_page)
        ))
        .order_by(order_clause)
      )
    ).all()

    items = []
    for row in rows_orm:
      v = row[0]
      logo_url = row[1]
      v_meta = getattr(v, "metadata_json", {}) or {}
      scoring = v_meta.get("scoring") or {}

      summary = v.summary_en if out_lang == "en" else (v.summary_ru or v.summary_en)
      contacts_dict = _contacts_list_to_dict(getattr(v, "contacts", None))

      derived_source_url = None
      try:
        tg_user = (getattr(v, "tg_channel_username", None) or "").lstrip("@")
        tg_msg_id = getattr(v, "tg_message_id", None)
        if tg_user and tg_msg_id:
          derived_source_url = f"https://t.me/{tg_user}/{tg_msg_id}"
      except Exception:
        pass

      company_data = None
      if v.company_id and row[2]:
        company_data = {
          "name": v.company_name,
          "website": row[2],
          "logo_url": logo_url,
          "industry": row[3],
          "size": row[4],
          "founded": row[5],
          "headquarters": row[6],
          "summary": row[7],
          "socials": row[8] or {},
          "domains": row[9] or [],
        }
      elif not company_data:
        cp = v_meta.get("company_profile")
        if cp and isinstance(cp, dict) and cp.get("summary"):
          company_data = {
            "name": v.company_name,
            "website": cp.get("website"),
            "logo_url": cp.get("logo_url") or logo_url,
            "industry": cp.get("industry"),
            "size": cp.get("size"),
            "founded": cp.get("founded"),
            "headquarters": cp.get("headquarters"),
            "summary": cp.get("summary"),
            "socials": {},
            "domains": [],
          }

      item = {
        "id": v.id,
        "title": v.title,
        "company_name": v.company_name,
        "role": v.role,
        "domains": v.domains or [],
        "risk_label": v.risk_label,
        "ai_score_value": getattr(v, "ai_score_value", None),
        "location_type": v.location_type,
        "salary_min_usd": v.salary_min_usd,
        "salary_max_usd": v.salary_max_usd,
        "currency": getattr(v, "currency", None),
        "seniority": getattr(v, "seniority", None),
        "role": getattr(v, "role", None),
        "english_level": getattr(v, "english_level", None),
        "employment_type": v_meta.get("employment_type"),
        "language_requirements": v_meta.get("language_requirements"),
        "experience_years": getattr(v, "experience_years", None),
        "country_city": getattr(v, "country_city", None),
        "recruiter": v.recruiter,
        "summary": summary,
        "skills": v.stack or [],
        "stack": v.stack or [],
        "contacts": contacts_dict,
        "source_url": v.source_url or derived_source_url,
        "source_channel": getattr(v, "source_channel", None),
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "scoring": {
          "total_score": scoring.get("total_score"),
          "overall_summary": scoring.get("overall_summary"),
          "red_flags": scoring.get("red_flags") or [],
          "scoring_results": scoring.get("scoring_results") or [],
        } if scoring else None,
        "company": company_data,
        "description": v.description,
        "responsibilities": v.responsibilities,
        "requirements": v.requirements,
        "conditions": v.conditions,
        "raw_text": v.raw_text,
      }
      items.append(item)

  return {"page": page, "per_page": per_page, "total": total, "items": items}


@app.get("/api/public/vacancies/semantic-search")
async def api_public_vacancies_semantic_search(
  request: Request,
  q: str = Query(..., min_length=1),
  page: int = Query(1, ge=1),
  per_page: int = Query(20, ge=1, le=200),
  domains: list[str] = Query([]),
  location_type: str | None = Query(None),
  seniority: str | None = Query(None),
  role: str | None = Query(None, description="Canonical role filter"),
  employment_type: str | None = Query(None),
  salary_min_usd: str | None = Query(None),
  salary_max_usd: str | None = Query(None),
  score_min: str | None = Query(None),
  score_max: str | None = Query(None),
  risk_label: str | None = Query(None),
  company_name: str | None = Query(None),
  search: str | None = Query(None),
  sort: str | None = Query(None, description="Sort: relevance (default), date_asc, date_desc, salary_asc, salary_desc, score_asc, score_desc"),
):
  """Semantic search with the same rich response as /api/public/vacancies."""
  api_key_token = (request.headers.get("X-API-Key") or "").strip()
  if not api_key_token:
    raise HTTPException(status_code=401, detail="Missing X-API-Key header")

  token_hash = _hash_api_key_token(api_key_token)
  now = datetime.utcnow()

  async with SessionLocal() as s:
    api_key = (
      await s.execute(
        select(ApiKey).where(
          ApiKey.api_key_hash == token_hash,
          ApiKey.is_active == True,  # noqa: E712
          or_(ApiKey.expires_at.is_(None), ApiKey.expires_at > now),
        )
      )
    ).scalar_one_or_none()
    if not api_key:
      raise HTTPException(status_code=401, detail="Invalid API key")

    await _enforce_api_key_limits_and_log(
      s=s, api_key=api_key, endpoint="/api/public/vacancies/semantic-search", status_code=200,
    )

    embedding = await embed_text(q)
    if not embedding:
      raise HTTPException(status_code=503, detail="Embeddings not available")

    vec = "[" + ",".join(f"{x:.6f}" for x in embedding) + "]"

    cfg = api_key.config or {}
    key_filters = cfg.get("filters") or {}
    out_lang = (cfg.get("output") or {}).get("language") or "en"

    q_domains = [str(d).strip().lower() for d in domains if str(d).strip()]
    if not q_domains:
      q_domains = [str(d).strip().lower() for d in (key_filters.get("domains") or []) if str(d).strip()]

    where = ["v.embedding IS NOT NULL"]
    params: dict = {"vec": vec, "limit": per_page, "offset": (page - 1) * per_page}

    if q_domains:
      domains_arr = "{" + ",".join(f'"{d}"' for d in q_domains) + "}"
      where.append("v.domains && (:domains_arr)::text[]")
      params["domains_arr"] = domains_arr

    q_location = (location_type or "").strip().lower() or (key_filters.get("location_type") or "").strip().lower() or None
    if q_location:
      where.append("v.location_type = :location_type")
      params["location_type"] = q_location

    q_risk = (risk_label or "").strip() or (key_filters.get("risk_label") or "").strip() or None
    if q_risk:
      if q_risk == "not-high-risk":
        where.append("(v.risk_label IS NULL OR v.risk_label != 'high-risk')")
      else:
        where.append("v.risk_label = :risk_label")
        params["risk_label"] = q_risk

    q_sen = (seniority or "").strip().lower() or None
    if q_sen:
      where.append("lower(v.seniority) = :seniority")
      params["seniority"] = q_sen

    q_role_ss = (role or "").strip() or None
    if q_role_ss:
      where.append("v.role = :role")
      params["role"] = q_role_ss

    q_emp = (employment_type or "").strip().lower() or None
    if q_emp:
      where.append("v.metadata->>'employment_type' = :employment_type")
      params["employment_type"] = q_emp

    _sal_min = _safe_int(salary_min_usd) if salary_min_usd else _safe_int(key_filters.get("salary_min_usd"))
    _sal_max = _safe_int(salary_max_usd) if salary_max_usd else _safe_int(key_filters.get("salary_max_usd"))
    if _sal_min is not None:
      where.append("v.salary_max_usd IS NOT NULL AND v.salary_max_usd >= :salary_min_usd")
      params["salary_min_usd"] = _sal_min
    if _sal_max is not None:
      where.append("v.salary_min_usd IS NOT NULL AND v.salary_min_usd <= :salary_max_usd")
      params["salary_max_usd"] = _sal_max

    _sc_min = _safe_int(score_min)
    _sc_max = _safe_int(score_max)
    if _sc_min is not None:
      where.append("v.ai_score_value >= :score_min")
      params["score_min"] = _sc_min
    if _sc_max is not None:
      where.append("v.ai_score_value <= :score_max")
      params["score_max"] = _sc_max

    role_f = key_filters.get("role")
    if role_f:
      where.append("v.role ILIKE '%' || :role || '%'")
      params["role"] = str(role_f).strip()
    recruiter_f = key_filters.get("recruiter")
    if recruiter_f:
      where.append("v.recruiter ILIKE '%' || :recruiter || '%'")
      params["recruiter"] = str(recruiter_f).strip()

    if company_name and company_name.strip():
      where.append("v.company_name ILIKE '%' || :company_name || '%'")
      params["company_name"] = company_name.strip()

    if search and search.strip():
      where.append("(v.title ILIKE '%' || :search || '%' OR v.company_name ILIKE '%' || :search || '%' OR v.raw_text ILIKE '%' || :search || '%')")
      params["search"] = search.strip()

    where_sql = " AND ".join(where) if where else "TRUE"

    _sort_key = (sort or "").strip().lower()
    _sort_sql_map = {
      "date_asc": "v.created_at ASC",
      "date_desc": "v.created_at DESC",
      "salary_asc": "v.salary_min_usd ASC NULLS LAST",
      "salary_desc": "v.salary_max_usd DESC NULLS LAST",
      "score_asc": "v.ai_score_value ASC NULLS LAST",
      "score_desc": "v.ai_score_value DESC NULLS LAST",
    }
    order_sql = _sort_sql_map.get(_sort_key, "v.embedding <=> (:vec)::vector ASC")

    sql_count = f"SELECT count(*) FROM vacancies v WHERE {where_sql}"
    sql_select = f"""
      SELECT
        v.id, v.title, v.company_name, v.role, v.domains, v.risk_label,
        v.ai_score_value, v.location_type, v.salary_min_usd, v.salary_max_usd,
        v.currency, v.recruiter, v.summary_en, v.summary_ru, v.contacts, v.source_url,
        v.source_channel, v.created_at, v.description, v.responsibilities, v.requirements,
        v.conditions, v.raw_text, v.stack, v.tg_channel_username, v.tg_message_id,
        v.seniority, v.role, v.english_level, v.experience_years, v.country_city,
        v.metadata AS metadata_json, v.company_id,
        c.logo_url AS company_logo_url, c.website AS company_website,
        c.industry AS company_industry, c.size AS company_size,
        c.founded AS company_founded, c.headquarters AS company_hq,
        c.summary AS company_summary, c.socials AS company_socials,
        c.domains AS company_domains,
        (1 - (v.embedding <=> (:vec)::vector)) AS semantic_similarity
      FROM vacancies v
      LEFT JOIN companies c ON c.id = v.company_id
      WHERE {where_sql}
      ORDER BY {order_sql}
      LIMIT :limit OFFSET :offset
    """

    total = (await s.execute(text(sql_count), params)).scalar() or 0
    rows = (await s.execute(text(sql_select), params)).mappings().all()

    items: list[dict] = []
    for row in rows:
      summary = row.get("summary_en") if out_lang == "en" else (row.get("summary_ru") or row.get("summary_en"))
      contacts_dict = _contacts_list_to_dict(row.get("contacts"))
      v_meta = row.get("metadata_json") or {}
      scoring = v_meta.get("scoring") or {}

      derived_source_url = None
      try:
        tg_user = (row.get("tg_channel_username") or "").lstrip("@")
        tg_msg_id = row.get("tg_message_id")
        if tg_user and tg_msg_id:
          derived_source_url = f"https://t.me/{tg_user}/{tg_msg_id}"
      except Exception:
        pass

      company_data = None
      if row.get("company_website"):
        company_data = {
          "name": row.get("company_name"),
          "website": row.get("company_website"),
          "logo_url": row.get("company_logo_url"),
          "industry": row.get("company_industry"),
          "size": row.get("company_size"),
          "founded": row.get("company_founded"),
          "headquarters": row.get("company_hq"),
          "summary": row.get("company_summary"),
          "socials": row.get("company_socials") or {},
          "domains": row.get("company_domains") or [],
        }
      elif not company_data:
        cp = v_meta.get("company_profile")
        if cp and isinstance(cp, dict) and cp.get("summary"):
          company_data = {
            "name": row.get("company_name"),
            "website": cp.get("website"),
            "logo_url": cp.get("logo_url"),
            "industry": cp.get("industry"),
            "size": cp.get("size"),
            "founded": cp.get("founded"),
            "headquarters": cp.get("headquarters"),
            "summary": cp.get("summary"),
            "socials": {},
            "domains": [],
          }

      items.append({
        "id": row.get("id"),
        "title": row.get("title"),
        "company_name": row.get("company_name"),
        "role": row.get("role"),
        "domains": row.get("domains") or [],
        "risk_label": row.get("risk_label"),
        "ai_score_value": row.get("ai_score_value"),
        "location_type": row.get("location_type"),
        "salary_min_usd": row.get("salary_min_usd"),
        "salary_max_usd": row.get("salary_max_usd"),
        "currency": row.get("currency"),
        "seniority": row.get("seniority"),
        "role": row.get("role"),
        "english_level": row.get("english_level"),
        "employment_type": v_meta.get("employment_type"),
        "language_requirements": v_meta.get("language_requirements"),
        "experience_years": row.get("experience_years"),
        "country_city": row.get("country_city"),
        "recruiter": row.get("recruiter"),
        "summary": summary,
        "skills": row.get("stack") or [],
        "stack": row.get("stack") or [],
        "contacts": contacts_dict,
        "source_url": row.get("source_url") or derived_source_url,
        "source_channel": row.get("source_channel"),
        "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
        "scoring": {
          "total_score": scoring.get("total_score"),
          "overall_summary": scoring.get("overall_summary"),
          "red_flags": scoring.get("red_flags") or [],
          "scoring_results": scoring.get("scoring_results") or [],
        } if scoring else None,
        "company": company_data,
        "description": row.get("description"),
        "responsibilities": row.get("responsibilities"),
        "requirements": row.get("requirements"),
        "conditions": row.get("conditions"),
        "raw_text": row.get("raw_text"),
        "semantic_similarity": float(row["semantic_similarity"]) if row.get("semantic_similarity") is not None else None,
      })

  return {"page": page, "per_page": per_page, "total": total, "items": items, "q": q}


@app.get("/api/public/vacancies/{vacancy_id}")
async def api_public_vacancy_detail(
  request: Request,
  vacancy_id: int,
):
  api_key_token = (request.headers.get("X-API-Key") or "").strip()
  if not api_key_token:
    raise HTTPException(status_code=401, detail="Missing X-API-Key header")

  token_hash = _hash_api_key_token(api_key_token)
  now = datetime.utcnow()
  async with SessionLocal() as s:
    api_key = (
      await s.execute(
        select(ApiKey).where(
          ApiKey.api_key_hash == token_hash,
          ApiKey.is_active == True,  # noqa: E712
          or_(ApiKey.expires_at.is_(None), ApiKey.expires_at > now),
        )
      )
    ).scalar_one_or_none()
    if not api_key:
      raise HTTPException(status_code=401, detail="Invalid API key")

    await _enforce_api_key_limits_and_log(
      s=s, api_key=api_key, endpoint=f"/api/public/vacancies/{vacancy_id}", status_code=200,
    )

    cfg = api_key.config or {}
    out_lang = (cfg.get("output") or {}).get("language") or "en"

    row = (
      await s.execute(
        select(Vacancy, Company.logo_url, Company.website.label("company_website_enriched"),
               Company.industry, Company.size.label("company_size_enriched"),
               Company.founded, Company.headquarters, Company.summary.label("company_summary"),
               Company.socials.label("company_socials"), Company.domains.label("company_domains"))
        .select_from(Vacancy)
        .outerjoin(Company, Company.id == Vacancy.company_id)
        .where(Vacancy.id == vacancy_id)
      )
    ).one_or_none()

    if not row:
      raise HTTPException(status_code=404, detail="Vacancy not found")

    v = row[0]
    logo_url = row[1]
    v_meta = getattr(v, "metadata_json", {}) or {}
    scoring = v_meta.get("scoring") or {}
    summary = v.summary_en if out_lang == "en" else (v.summary_ru or v.summary_en)
    contacts_dict = _contacts_list_to_dict(getattr(v, "contacts", None))

    derived_source_url = None
    try:
      tg_user = (getattr(v, "tg_channel_username", None) or "").lstrip("@")
      tg_msg_id = getattr(v, "tg_message_id", None)
      if tg_user and tg_msg_id:
        derived_source_url = f"https://t.me/{tg_user}/{tg_msg_id}"
    except Exception:
      pass

    company_data = None
    if v.company_id and row[2]:
      company_data = {
        "name": v.company_name,
        "website": row[2],
        "logo_url": logo_url,
        "industry": row[3],
        "size": row[4],
        "founded": row[5],
        "headquarters": row[6],
        "summary": row[7],
        "socials": row[8] or {},
        "domains": row[9] or [],
      }
    elif not company_data:
      cp = v_meta.get("company_profile")
      if cp and isinstance(cp, dict) and cp.get("summary"):
        company_data = {
          "name": v.company_name,
          "website": cp.get("website"),
          "logo_url": cp.get("logo_url") or logo_url,
          "industry": cp.get("industry"),
          "size": cp.get("size"),
          "founded": cp.get("founded"),
          "headquarters": cp.get("headquarters"),
          "summary": cp.get("summary"),
          "socials": {},
          "domains": [],
        }

    return {
      "id": v.id,
      "title": v.title,
      "company_name": v.company_name,
      "role": getattr(v, "role", None),
      "domains": v.domains or [],
      "risk_label": v.risk_label,
      "ai_score_value": getattr(v, "ai_score_value", None),
      "location_type": v.location_type,
      "salary_min_usd": v.salary_min_usd,
      "salary_max_usd": v.salary_max_usd,
      "currency": getattr(v, "currency", None),
      "seniority": getattr(v, "seniority", None),
      "english_level": getattr(v, "english_level", None),
      "employment_type": v_meta.get("employment_type"),
      "language_requirements": v_meta.get("language_requirements"),
      "experience_years": getattr(v, "experience_years", None),
      "country_city": getattr(v, "country_city", None),
      "recruiter": v.recruiter,
      "summary": summary,
      "skills": v.stack or [],
      "stack": v.stack or [],
      "contacts": contacts_dict,
      "source_url": v.source_url or derived_source_url,
      "source_channel": getattr(v, "source_channel", None),
      "created_at": v.created_at.isoformat() if v.created_at else None,
      "scoring": {
        "total_score": scoring.get("total_score"),
        "overall_summary": scoring.get("overall_summary"),
        "red_flags": scoring.get("red_flags") or [],
        "scoring_results": scoring.get("scoring_results") or [],
      } if scoring else None,
      "company": company_data,
      "description": v.description,
      "responsibilities": v.responsibilities,
      "requirements": v.requirements,
      "conditions": v.conditions,
      "raw_text": v.raw_text,
    }


@app.get("/api/vacancies/suggest")
async def vacancy_suggest(
  q: str = Query("", min_length=0),
):
  """Fast autocomplete: return up to 7 matches by title/company_name."""
  term = (q or "").strip()
  if len(term) < 2:
    return []
  like = f"%{term}%"
  async with SessionLocal() as s:
    rows = (
      await s.execute(
        select(Vacancy.id, Vacancy.title, Vacancy.company_name, Vacancy.domains, Vacancy.created_at)
        .where(
          or_(
            Vacancy.title.ilike(like),
            Vacancy.company_name.ilike(like),
          )
        )
        .order_by(desc(Vacancy.id))
        .limit(7)
      )
    ).all()
  return [
    {
      "id": r.id,
      "title": r.title or "Untitled",
      "company_name": r.company_name,
      "domains": r.domains or [],
      "created_at": r.created_at.isoformat() if r.created_at else None,
    }
    for r in rows
  ]


@app.get("/api/vacancies/{vacancy_id}")
async def get_vacancy_details(request: Request, vacancy_id: int):
  async with SessionLocal() as s:
    v = (await s.execute(select(Vacancy).where(Vacancy.id == vacancy_id))).scalar_one_or_none()
    if not v:
      raise HTTPException(status_code=404, detail="Vacancy not found")
    metadata = getattr(v, "metadata_json", {}) or {}
    scoring = metadata.get("scoring") or {}

    if not scoring and metadata.get("ai_score_points"):
      old_score_100 = metadata.get("ai_score_100")
      old_10 = None
      if old_score_100 is not None:
        try: old_10 = round(int(old_score_100) / 10.0, 1)
        except Exception: pass
      if old_10 is None and getattr(v, "ai_score_value", None) is not None:
        try: old_10 = float(v.ai_score_value)
        except Exception: old_10 = 5.0
      scoring = {
        "total_score": old_10 or 5.0,
        "overall_summary": "",
        "red_flags": [],
        "scoring_results": [],
      }

    if not scoring:
      sc_val = getattr(v, "ai_score_value", None)
      scoring = {
        "total_score": float(sc_val) if sc_val is not None else 5.0,
        "overall_summary": "",
        "red_flags": [],
        "scoring_results": [],
      }

    # Company data from companies table (preferred) or metadata fallback
    company_data = None
    related_vacancies = []
    comp_id = getattr(v, "company_id", None)
    if comp_id:
      comp = (await s.execute(select(Company).where(Company.id == comp_id))).scalar_one_or_none()
      if comp:
        company_data = {
          "id": comp.id,
          "name": comp.name,
          "website": comp.website,
          "linkedin": comp.linkedin,
          "logo_url": comp.logo_url,
          "summary": comp.summary,
          "industry": comp.industry,
          "size": comp.size,
          "founded": comp.founded,
          "headquarters": comp.headquarters,
          "domains": comp.domains or [],
          "socials": comp.socials or {},
        }
        # Related vacancies from the same company
        from sqlalchemy import and_
        rows = (await s.execute(
          select(Vacancy.id, Vacancy.title, Vacancy.created_at, Vacancy.ai_score_value)
          .where(and_(Vacancy.company_id == comp_id, Vacancy.id != vacancy_id))
          .order_by(Vacancy.created_at.desc())
          .limit(10)
        )).all()
        for r in rows:
          related_vacancies.append({
            "id": r[0], "title": r[1],
            "created_at": r[2].isoformat() if r[2] else None,
            "ai_score_value": r[3],
          })

    if not company_data:
      cp = (metadata or {}).get("company_profile")
      if cp and isinstance(cp, dict):
        company_data = {
          "name": v.company_name,
          "website": cp.get("website") or getattr(v, "company_url", None),
          "logo_url": cp.get("logo_url"),
          "summary": cp.get("summary"),
          "industry": cp.get("industry"),
          "size": cp.get("size"),
          "founded": cp.get("founded"),
          "headquarters": cp.get("headquarters"),
          "domains": [],
        }

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
      "raw_text": v.raw_text if _is_authenticated(request) else None,
      "source_url": v.source_url,
      "created_at": v.created_at.isoformat() if v.created_at else None,
      "scoring": scoring,
      "company_url": getattr(v, "company_url", None),
      "company_linkedin": (metadata or {}).get("company_linkedin"),
      "company_url_verified": (metadata or {}).get("company_url_verified", False),
      "company_linkedin_verified": (metadata or {}).get("company_linkedin_verified", False),
      "seniority": getattr(v, "seniority", None),
      "role": getattr(v, "role", None),
      "english_level": getattr(v, "english_level", None),
      "employment_type": (metadata or {}).get("employment_type"),
      "language_requirements": (metadata or {}).get("language_requirements"),
      "source_channel": getattr(v, "source_channel", None) if _is_authenticated(request) else None,
      "external_id": getattr(v, "external_id", None),
      "tg_channel_username": getattr(v, "tg_channel_username", None) if _is_authenticated(request) else None,
      "company": company_data,
      "related_vacancies": related_vacancies,
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
    # Try to enrich from ATS if contacts contain an apply URL
    apply_url = None
    for c in (v.contacts or []):
      if isinstance(c, str) and c.startswith("http"):
        apply_url = c
        break
    if not apply_url and v.source_url and v.source_url.startswith("http"):
      apply_url = v.source_url

  try:
    text_raw = await try_enrich_from_ats(text_raw, apply_url)
    analysis = await analyze_with_openrouter(text_raw)
    # Resolve company first so heuristic scoring can factor in verified URLs.
    _ci = await resolve_company_info(
      company_name=analysis.get("company_name"),
      raw_text=text_raw,
      llm_website=analysis.get("company_website"),
      llm_linkedin=analysis.get("company_linkedin"),
    )
    analysis["_company_url_verified"] = _ci.get("company_url_verified", False)
    analysis["_company_linkedin_verified"] = _ci.get("company_linkedin_verified", False)
    scoring = await score_vacancy_with_openrouter(text_raw, analysis)
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

  total_score = scoring.get("total_score")
  try:
    ai_score_value_0_10 = int(round(float(total_score)))
  except Exception:
    ai_score_value_0_10 = int(analysis.get("ai_score_value") or 5)
  ai_score_value_0_10 = max(0, min(10, ai_score_value_0_10))

  company_profile = {}
  try:
    company_profile = await enrich_company_profile(
      company_name=analysis.get("company_name"),
      company_url=_ci.get("company_url"),
    )
  except Exception:
    pass

  scoring = _boost_company_score(scoring, company_profile, _ci)
  total_score = scoring.get("total_score")
  try:
    ai_score_value_0_10 = int(round(float(total_score)))
  except Exception:
    pass
  ai_score_value_0_10 = max(0, min(10, ai_score_value_0_10))

  display_company_url = pick_corporate_website(_ci.get("company_url"), company_profile.get("website"))

  company_id = await upsert_company(
    company_name=analysis.get("company_name"),
    company_profile=company_profile,
    company_url=_ci.get("company_url"),
    company_linkedin=_ci.get("company_linkedin"),
  )
  vacancy_domains = [str(x).strip().lower() for x in (analysis.get("domains") or []) if str(x).strip()]
  if company_id:
    try:
      async with SessionLocal() as s:
        comp = (await s.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
        if comp and comp.domains:
          for cd in comp.domains:
            if cd and cd.lower() not in vacancy_domains:
              vacancy_domains.append(cd.lower())
    except Exception:
      pass

  async with SessionLocal() as s:
    old = (await s.execute(select(Vacancy).where(Vacancy.id == vacancy_id))).scalar_one_or_none()

    old_meta = getattr(old, "metadata_json", {}) if old else {}
    old_meta = old_meta or {}
    # Preserve all original contacts (apply URL, email, telegram) — never drop them on re-analysis
    old_contacts = list(old.contacts or []) if old else []
    old_source_url = (getattr(old, "source_url", None) or "") if old else ""
    new_meta = {
      **old_meta,
      **(analysis.get("metadata", {}) or {}),
      "scoring": scoring,
      "company_linkedin": _ci.get("company_linkedin"),
      "company_url_verified": _ci.get("company_url_verified", False),
      "company_linkedin_verified": _ci.get("company_linkedin_verified", False),
      "employment_type": analysis.get("employment_type"),
      "language_requirements": analysis.get("language_requirements"),
      "company_profile": company_profile if company_profile else None,
    }
    merged_contacts = _enrich_contacts_with_forms(analysis.get("contacts") or [], text_raw)
    _mc_lower = {c.lower() for c in merged_contacts}
    # Merge old contacts (never drop apply URL, email, telegram handle)
    for _c in old_contacts:
      if _c and _c.lower() not in _mc_lower:
        merged_contacts.append(_c)
        _mc_lower.add(_c.lower())
    # Also ensure source_url (ATS apply link) is always in contacts as last resort
    if apply_url and apply_url.lower() not in _mc_lower:
      merged_contacts.append(apply_url)
    elif old_source_url and old_source_url.startswith("http") and old_source_url.lower() not in _mc_lower:
      merged_contacts.append(old_source_url)
    # Strip the source Telegram channel — it's the aggregator, not a recruiter contact
    tg_chan = getattr(old, "tg_channel_username", None) if old else None
    merged_contacts = _strip_channel_from_contacts(merged_contacts, tg_chan)
    await s.execute(
      update(Vacancy)
      .where(Vacancy.id == vacancy_id)
      .values(
        raw_text=text_raw,
        company_name=analysis.get("company_name"),
        company_url=display_company_url,
        title=analysis.get("title"),
        location_type=(analysis.get("location_type") or "").lower().strip() or None,
        salary_min_usd=analysis.get("salary_min_usd"),
        salary_max_usd=analysis.get("salary_max_usd"),
        stack=analysis.get("stack") or [],
        ai_score_value=ai_score_value_0_10,
        summary_en=analysis.get("summary_en"),
        summary_ru=analysis.get("summary_ru"),
        metadata_json=new_meta,
        domains=vacancy_domains,
        risk_label=analysis.get("risk_label"),
        recruiter=analysis.get("recruiter"),
        contacts=merged_contacts,
        description=analysis.get("description"),
        responsibilities=analysis.get("responsibilities"),
        requirements=analysis.get("requirements"),
        conditions=analysis.get("conditions"),
        role=analysis.get("role"),
        seniority=(analysis.get("seniority") or "").lower().strip() or None,
        english_level=(analysis.get("english_level") or "").strip().upper() or None,
        standardized_title=analysis.get("standardized_title"),
        language=analysis.get("language"),
        company_id=company_id,
      )
    )
    await s.commit()

  return {"success": True}


@app.post("/api/vacancies/bulk-reenrich")
async def bulk_reenrich_vacancies(
  background_tasks: BackgroundTasks,
  _: bool = Depends(require_auth),
  company_name: str | None = Query(None, description="Filter by company name (case-insensitive)"),
  max_desc_len: int = Query(500, description="Re-enrich if description+responsibilities+requirements < N chars"),
  limit: int = Query(100, le=500, description="Max vacancies to queue"),
):
  """
  Find vacancies with short descriptions that have an apply URL, then queue them for re-analysis.
  Designed to retroactively enrich aggregator-scraped vacancies (Coinbase, Binance, etc.)
  that were stored with stub descriptions from the aggregator instead of the real ATS content.
  """
  import structlog as _slog
  _log = _slog.get_logger()

  async with SessionLocal() as s:
    q = select(
      Vacancy.id,
      Vacancy.contacts,
      Vacancy.description,
      Vacancy.responsibilities,
      Vacancy.requirements,
    )
    if company_name:
      q = q.where(func.lower(Vacancy.company_name) == company_name.strip().lower())
    # Over-fetch then filter by description length in Python
    q = q.order_by(Vacancy.id.desc()).limit(limit * 10)
    rows = (await s.execute(q)).all()

  ids_to_process: list[int] = []
  for row in rows:
    desc_len = (
      len(row.description or "")
      + len(row.responsibilities or "")
      + len(row.requirements or "")
    )
    if desc_len >= max_desc_len:
      continue
    has_url = any(
      isinstance(c, str) and c.startswith("http")
      for c in (row.contacts or [])
    )
    if not has_url:
      continue
    ids_to_process.append(row.id)
    if len(ids_to_process) >= limit:
      break

  async def _process_all(ids: list[int]) -> None:
    for vid in ids:
      try:
        await reanalyze_vacancy(vid, _=True)
        _log.info("bulk_reenrich_ok", vacancy_id=vid)
      except Exception as exc:
        _log.warning("bulk_reenrich_failed", vacancy_id=vid, error=str(exc))

  background_tasks.add_task(_process_all, ids_to_process)
  return {"queued": len(ids_to_process), "ids": ids_to_process}


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
  _now = datetime.now(timezone.utc)
  _day7 = _now - timedelta(days=7)

  async with SessionLocal() as s:
    query = (
      select(
        Channel,
        func.count(Vacancy.id).label("vacancies_count"),
        func.avg(Vacancy.ai_score_value).label("avg_score"),
        func.coalesce(func.sum(case((Vacancy.created_at >= _day7, 1), else_=0)), 0).label("vacancies_7d"),
      )
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

    # Sparkline: vacancies per day for last 30 days grouped by tg_channel_username
    sparkline_data: dict = {}
    sl_rows = (await s.execute(text(
      "SELECT tg_channel_username, DATE(created_at) AS day, COUNT(*) AS cnt "
      "FROM vacancies "
      "WHERE created_at >= NOW() - INTERVAL '30 days' "
      "  AND tg_channel_username IS NOT NULL "
      "GROUP BY tg_channel_username, DATE(created_at)"
    ))).all()
    for uname, day_val, cnt in sl_rows:
      if uname:
        day_key = day_val.isoformat() if hasattr(day_val, "isoformat") else str(day_val)
        sparkline_data.setdefault(uname, {})[day_key] = int(cnt)

    # Web sources with analytics
    web_sources_raw = (await s.execute(
      select(WebSource).order_by(desc(WebSource.created_at))
    )).scalars().all()
    web_sources = []
    for ws in web_sources_raw:
      sc = f"web:{ws.slug}"
      sc_filter = Vacancy.source_channel == sc
      vac_count = (await s.execute(select(func.count(Vacancy.id)).where(sc_filter))).scalar() or 0
      avg_sc = (await s.execute(select(func.avg(Vacancy.ai_score_value)).where(sc_filter))).scalar()
      vac_7d = (await s.execute(
        select(func.count(Vacancy.id)).where(sc_filter, Vacancy.created_at >= _day7)
      )).scalar() or 0
      ws_sl_rows = (await s.execute(text(
        "SELECT DATE(created_at) AS day, COUNT(*) AS cnt "
        "FROM vacancies WHERE source_channel = :sc "
        "AND created_at >= NOW() - INTERVAL '14 days' "
        "GROUP BY DATE(created_at)"
      ).bindparams(sc=sc))).all()
      sparkline_data[sc] = {
        (r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0])): int(r[1])
        for r in ws_sl_rows if r[0]
      }
      web_sources.append({
        "id": ws.id,
        "slug": ws.slug,
        "name": ws.name,
        "url": ws.url,
        "parser_type": ws.parser_type,
        "enabled": ws.enabled,
        "sync_interval_minutes": ws.sync_interval_minutes,
        "last_synced_at": ws.last_synced_at,
        "vacancies_count": vac_count,
        "avg_score": round(float(avg_sc), 1) if avg_sc is not None else None,
        "vacancies_7d": vac_7d,
        "sparkline_key": sc,
        "created_at": ws.created_at,
      })

  return templates.TemplateResponse(
    request, "channels.html",
    {
      "channels": channels,
      "web_sources": web_sources,
      "filters": {
        "search": search or "",
        "enabled": enabled or "",
        "domain": domain or "",
        "risk": risk or "",
        "sort": sort or "created_desc",
      },
      "domain_options": domain_options,
      "sparkline_data": json.dumps(sparkline_data),
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
    ch_conditions = []
    if ch.tg_id is not None:
      ch_conditions.append(Vacancy.tg_channel_id == ch.tg_id)
    if ch.username:
      ch_conditions.append(Vacancy.tg_channel_username == ch.username)
      ch_conditions.append(Vacancy.source_channel == ch.username)
    if ch_conditions:
      ch_filter = or_(*ch_conditions)
      total_vac = (await s.execute(select(func.count(Vacancy.id)).where(ch_filter))).scalar() or 0
      latest = (
        await s.execute(
          select(Vacancy)
          .where(ch_filter)
          .order_by(desc(Vacancy.id))
          .limit(20)
        )
      ).scalars().all()
    else:
      total_vac = 0
      latest = []

  def _vac_dict(v):
    meta = v.metadata_json or {}
    scoring = meta.get("scoring", {}) if isinstance(meta, dict) else {}
    ts = scoring.get("total_score") if isinstance(scoring, dict) else None
    score = float(ts) if ts is not None else (float(v.ai_score_value) if getattr(v, "ai_score_value", None) is not None else None)
    return {
      "id": v.id,
      "title": v.title,
      "company_name": v.company_name,
      "score": score,
      "domains": v.domains or [],
      "location_type": v.location_type,
      "seniority": getattr(v, "seniority", None),
      "role": getattr(v, "role", None),
      "created_at": v.created_at.isoformat() if v.created_at else None,
    }

  return {
    "id": ch.id,
    "tg_id": ch.tg_id,
    "username": ch.username,
    "title": ch.title,
    "bio": ch.bio,
    "members_count": ch.members_count,
    "enabled": ch.enabled,
    "created_at": ch.created_at.isoformat() if ch.created_at else None,
    "ai_domains": getattr(ch, "ai_domains", []) or [],
    "ai_tags": getattr(ch, "ai_tags", []) or [],
    "ai_risk_label": getattr(ch, "ai_risk_label", None),
    "total_vacancies": total_vac,
    "latest_vacancies": [_vac_dict(v) for v in latest],
  }


@app.get("/api/channels/{channel_id}/analytics")
async def channel_analytics(channel_id: int, _: bool = Depends(require_auth)):
  async with SessionLocal() as s:
    ch = (await s.execute(select(Channel).where(Channel.id == channel_id))).scalar_one_or_none()
    if not ch:
      raise HTTPException(status_code=404)
    _now = datetime.now(timezone.utc)
    _d7 = _now - timedelta(days=7)
    _d30 = _now - timedelta(days=30)
    cond = []
    if ch.tg_id: cond.append(Vacancy.tg_channel_id == ch.tg_id)
    if ch.username:
      cond.append(Vacancy.tg_channel_username == ch.username)
      cond.append(Vacancy.source_channel == ch.username)
    if not cond:
      return {"total": 0, "count_7d": 0, "avg_score": None, "daily": {}, "score_dist": {}}
    flt = or_(*cond)
    total = (await s.execute(select(func.count(Vacancy.id)).where(flt))).scalar() or 0
    count_7d = (await s.execute(select(func.count(Vacancy.id)).where(flt, Vacancy.created_at >= _d7))).scalar() or 0
    avg_sc = (await s.execute(select(func.avg(Vacancy.ai_score_value)).where(flt))).scalar()
    _d14 = _now - timedelta(days=14)
    count_prev_7d = (await s.execute(select(func.count(Vacancy.id)).where(flt, Vacancy.created_at >= _d14, Vacancy.created_at < _d7))).scalar() or 0
    daily_rows = (await s.execute(text(
      "SELECT DATE(created_at) AS day, COUNT(*) AS cnt, AVG(ai_score_value) AS avg_sc FROM vacancies "
      "WHERE tg_channel_username = :u AND created_at >= :d30 "
      "GROUP BY DATE(created_at) ORDER BY day"
    ).bindparams(u=ch.username or "", d30=_d30))).all()
    daily = {r[0].isoformat(): int(r[1]) for r in daily_rows if r[0]}
    daily_avg_score = {r[0].isoformat(): round(float(r[2]),2) for r in daily_rows if r[0] and r[2] is not None}
    score_rows = (await s.execute(text(
      "SELECT FLOOR(ai_score_value)::int AS bucket, COUNT(*) AS cnt FROM vacancies "
      "WHERE tg_channel_username = :u AND ai_score_value IS NOT NULL "
      "GROUP BY bucket ORDER BY bucket"
    ).bindparams(u=ch.username or ""))).all()
    score_dist = {str(r[0]): int(r[1]) for r in score_rows if r[0] is not None}
    top_roles_rows = (await s.execute(text(
      "SELECT role, COUNT(*) AS cnt FROM vacancies "
      "WHERE tg_channel_username = :u AND role IS NOT NULL "
      "GROUP BY role ORDER BY cnt DESC LIMIT 6"
    ).bindparams(u=ch.username or ""))).all()
    top_roles = [{"role": r[0], "count": int(r[1])} for r in top_roles_rows]
  return {
    "total": total, "count_7d": count_7d, "count_prev_7d": count_prev_7d,
    "avg_score": round(float(avg_sc), 2) if avg_sc is not None else None,
    "daily": daily, "daily_avg_score": daily_avg_score,
    "score_dist": score_dist, "top_roles": top_roles,
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


def _charts_json_for_template(charts: dict) -> Markup:
  """Embed chart data in <script> without relying on Jinja's tojson filter."""
  s = json.dumps(charts, ensure_ascii=True, default=str, separators=(",", ":"))
  s = s.replace("<", "\\u003c").replace(">", "\\u003e")
  return Markup(s)


def _safe_int_count(x: object) -> int:
  try:
    return int(x)  # type: ignore[arg-type]
  except (TypeError, ValueError):
    return 0


def _empty_analytics_charts() -> dict:
  z = {"labels": [], "counts": []}
  return {
    "domains": dict(z),
    "timeline": dict(z),
    "sources": dict(z),
    "seniority": dict(z),
    "roles": dict(z),
    "location": dict(z),
    "employment": dict(z),
    "titles": dict(z),
    "risk": dict(z),
    "scores": dict(z),
    "english": dict(z),
    "salary_domains": {"labels": [], "avg_mid": []},
    "salary_roles": {"labels": [], "avg_min": [], "avg_max": []},
  }


async def _pg_table_columns(s, table_name: str) -> set[str]:
  rows = (
    await s.execute(
      text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = :t"
      ),
      {"t": table_name},
    )
  ).all()
  return {r[0] for r in rows}


async def _exec_text_all(s, sql: str, params: dict | None = None):
  return (await s.execute(text(sql), params or {})).all()


# ── Web Sources API ──────────────────────────────────────────────
@app.get("/api/web-sources")
async def list_web_sources(_: bool = Depends(require_auth)):
  async with SessionLocal() as s:
    rows = (await s.execute(
      select(WebSource).order_by(desc(WebSource.created_at))
    )).scalars().all()
    result = []
    for ws in rows:
      vac_count = (await s.execute(
        select(func.count(Vacancy.id)).where(Vacancy.source_channel == f"web:{ws.slug}")
      )).scalar() or 0
      result.append({
        "id": ws.id,
        "slug": ws.slug,
        "name": ws.name,
        "url": ws.url,
        "parser_type": ws.parser_type,
        "enabled": ws.enabled,
        "sync_interval_minutes": ws.sync_interval_minutes,
        "max_pages": ws.max_pages,
        "last_synced_at": ws.last_synced_at.isoformat() if ws.last_synced_at else None,
        "vacancies_count": vac_count,
        "created_at": ws.created_at.isoformat() if ws.created_at else None,
      })
  return result


@app.post("/api/web-sources/{source_id}/sync")
async def trigger_web_source_sync(source_id: int, _: bool = Depends(require_auth)):
  async with SessionLocal() as s:
    ws = (await s.execute(
      select(WebSource).where(WebSource.id == source_id)
    )).scalar_one_or_none()
    if not ws:
      raise HTTPException(status_code=404, detail="Web source not found")
    slug = ws.slug
    parser_type = ws.parser_type
    max_pages = ws.max_pages

  if parser_type == "degencryptojobs":
    result = await degen_sync(max_pages=max_pages, limit=20)
  elif parser_type == "web3career":
    result = await w3c_sync(max_pages=max_pages, limit=20)
  elif parser_type == "cryptojobs":
    result = await cj_sync(max_pages=max_pages, limit=10)
  elif parser_type == "remocate":
    result = await remo_sync(max_pages=max_pages, limit=15)
  elif parser_type == "cryptocurrencyjobs_co":
    result = await ccj_sync(max_pages=max_pages, limit=25)
  elif parser_type == "sailonchain":
    result = await sail_sync(max_pages=max_pages, limit=20)
  else:
    raise HTTPException(status_code=400, detail=f"Unknown parser type: {parser_type}")

  return result


@app.post("/api/web-sources/{source_id}/toggle")
async def toggle_web_source(source_id: int, _: bool = Depends(require_auth)):
  async with SessionLocal() as s:
    ws = (await s.execute(
      select(WebSource).where(WebSource.id == source_id)
    )).scalar_one_or_none()
    if not ws:
      raise HTTPException(status_code=404, detail="Web source not found")
    ws.enabled = not ws.enabled
    new_state = ws.enabled
    await s.commit()
  return {"ok": True, "enabled": new_state}


@app.get("/api/web-sources/{source_id}/vacancies")
async def web_source_vacancies(
  source_id: int,
  _: bool = Depends(require_auth),
  page: int = Query(1, ge=1),
  per_page: int = Query(20, ge=1, le=100),
):
  async with SessionLocal() as s:
    ws = (await s.execute(
      select(WebSource).where(WebSource.id == source_id)
    )).scalar_one_or_none()
    if not ws:
      raise HTTPException(status_code=404, detail="Web source not found")

    source_channel = f"web:{ws.slug}"
    total = (await s.execute(
      select(func.count(Vacancy.id)).where(Vacancy.source_channel == source_channel)
    )).scalar() or 0

    vacancies = (await s.execute(
      select(Vacancy)
      .where(Vacancy.source_channel == source_channel)
      .order_by(desc(Vacancy.id))
      .offset((page - 1) * per_page)
      .limit(per_page)
    )).scalars().all()

    items = []
    for v in vacancies:
      items.append({
        "id": v.id,
        "title": v.title,
        "company_name": v.company_name,
        "source_url": v.source_url,
        "ai_score_value": v.ai_score_value,
        "salary_min_usd": v.salary_min_usd,
        "salary_max_usd": v.salary_max_usd,
        "domains": list(v.domains or []),
        "seniority": v.seniority,
        "location_type": v.location_type,
        "created_at": v.created_at.isoformat() if v.created_at else None,
      })

  return {"total": total, "page": page, "per_page": per_page, "items": items}


@app.get("/api/web-sources/{source_id}/analytics")
async def web_source_analytics(source_id: int, _: bool = Depends(require_auth)):
  async with SessionLocal() as s:
    ws = (await s.execute(select(WebSource).where(WebSource.id == source_id))).scalar_one_or_none()
    if not ws:
      raise HTTPException(status_code=404)
    sc = f"web:{ws.slug}"
    _now = datetime.now(timezone.utc)
    _d7  = _now - timedelta(days=7)
    _d30 = _now - timedelta(days=30)
    flt = Vacancy.source_channel == sc
    total    = (await s.execute(select(func.count(Vacancy.id)).where(flt))).scalar() or 0
    count_7d = (await s.execute(select(func.count(Vacancy.id)).where(flt, Vacancy.created_at >= _d7))).scalar() or 0
    avg_sc   = (await s.execute(select(func.avg(Vacancy.ai_score_value)).where(flt))).scalar()
    _d14 = _now - timedelta(days=14)
    count_prev_7d = (await s.execute(select(func.count(Vacancy.id)).where(flt, Vacancy.created_at >= _d14, Vacancy.created_at < _d7))).scalar() or 0
    daily_rows = (await s.execute(text(
      "SELECT DATE(created_at) AS day, COUNT(*) AS cnt, AVG(ai_score_value) AS avg_sc FROM vacancies "
      "WHERE source_channel = :sc AND created_at >= :d30 "
      "GROUP BY DATE(created_at) ORDER BY day"
    ).bindparams(sc=sc, d30=_d30))).all()
    daily = {r[0].isoformat(): int(r[1]) for r in daily_rows if r[0]}
    daily_avg_score = {r[0].isoformat(): round(float(r[2]),2) for r in daily_rows if r[0] and r[2] is not None}
    score_rows = (await s.execute(text(
      "SELECT FLOOR(ai_score_value)::int AS bucket, COUNT(*) AS cnt FROM vacancies "
      "WHERE source_channel = :sc AND ai_score_value IS NOT NULL "
      "GROUP BY bucket ORDER BY bucket"
    ).bindparams(sc=sc))).all()
    score_dist = {str(r[0]): int(r[1]) for r in score_rows if r[0] is not None}
    top_roles_rows = (await s.execute(text(
      "SELECT role, COUNT(*) AS cnt FROM vacancies "
      "WHERE source_channel = :sc AND role IS NOT NULL "
      "GROUP BY role ORDER BY cnt DESC LIMIT 6"
    ).bindparams(sc=sc))).all()
    top_roles = [{"role": r[0], "count": int(r[1])} for r in top_roles_rows]
  return {
    "total": total, "count_7d": count_7d, "count_prev_7d": count_prev_7d,
    "avg_score": round(float(avg_sc), 2) if avg_sc is not None else None,
    "daily": daily, "daily_avg_score": daily_avg_score,
    "score_dist": score_dist, "top_roles": top_roles,
  }


# ─── Analytics Studio ────────────────────────────────────────────────────────

_STUDIO_ALLOWED_FIELDS_VAC = {
    "id", "company_name", "title", "standardized_title", "location_type",
    "salary_min_usd", "salary_max_usd", "role", "seniority", "domain",
    "risk_label", "english_level", "country_city", "company_size",
    "experience_years", "source_channel", "tg_channel_username",
    "ai_score_value", "created_at", "category", "company_id",
}
_STUDIO_ALLOWED_FIELDS_COMP = {
    "id", "name", "website", "industry", "size", "headquarters",
    "founded", "created_at", "updated_at",
}
_STUDIO_ALLOWED_AGG = {"count", "avg", "sum", "min", "max"}
_STUDIO_DATE_TRUNC = {"day", "week", "month", "quarter", "year"}


def _validate_field(field: str, table: str = "vacancies") -> str:
    allowed = _STUDIO_ALLOWED_FIELDS_VAC if table == "vacancies" else _STUDIO_ALLOWED_FIELDS_COMP
    if field not in allowed:
        raise HTTPException(400, f"Field '{field}' not allowed")
    return field


@app.get("/analytics/studio", response_class=HTMLResponse)
async def analytics_studio_page(request: Request):
    return templates.TemplateResponse(request, "analytics_studio.html", {})


@app.get("/api/analytics/facets")
async def analytics_facets():
    """Dynamic filter options from the database."""
    async with SessionLocal() as s:
        def _col(q):
            return [r[0] for r in q.all() if r[0]]

        roles = _col(await s.execute(text(
            "SELECT DISTINCT role FROM vacancies WHERE role IS NOT NULL ORDER BY role")))
        domains = _col(await s.execute(text(
            "SELECT DISTINCT unnest(domains) AS d FROM vacancies ORDER BY d")))
        seniority = _col(await s.execute(text(
            "SELECT DISTINCT seniority FROM vacancies WHERE seniority IS NOT NULL ORDER BY seniority")))
        locations = _col(await s.execute(text(
            "SELECT DISTINCT location_type FROM vacancies WHERE location_type IS NOT NULL ORDER BY location_type")))
        companies = _col(await s.execute(text(
            "SELECT DISTINCT company_name FROM vacancies WHERE company_name IS NOT NULL ORDER BY company_name LIMIT 500")))
        skills = _col(await s.execute(text(
            "SELECT DISTINCT unnest(stack) AS s FROM vacancies ORDER BY s LIMIT 500")))
        sources = _col(await s.execute(text(
            "SELECT DISTINCT COALESCE(source_channel, tg_channel_username) AS src FROM vacancies "
            "WHERE COALESCE(source_channel, tg_channel_username) IS NOT NULL ORDER BY src")))
        risk_labels = _col(await s.execute(text(
            "SELECT DISTINCT risk_label FROM vacancies WHERE risk_label IS NOT NULL ORDER BY risk_label")))
        english = _col(await s.execute(text(
            "SELECT DISTINCT english_level FROM vacancies WHERE english_level IS NOT NULL ORDER BY english_level")))
        employment = _col(await s.execute(text(
            "SELECT DISTINCT metadata->>'employment_type' AS et FROM vacancies "
            "WHERE metadata->>'employment_type' IS NOT NULL ORDER BY et")))

    return {
        "roles": roles, "domains": domains, "seniority": seniority,
        "location_types": locations, "companies": companies,
        "skills": skills, "sources": sources, "risk_labels": risk_labels,
        "english_levels": english, "employment_types": employment,
    }


@app.post("/api/analytics/execute")
async def analytics_execute(request: Request):
    """Execute a visual pipeline and return tabular + chart-ready data."""
    body = await request.json()
    nodes = body.get("nodes", [])
    edges = body.get("edges", [])

    if not nodes:
        raise HTTPException(400, "Pipeline has no nodes")

    node_map = {n["id"]: n for n in nodes}
    children = {}
    parents = {}
    for e in edges:
        children.setdefault(e["from"], []).append(e["to"])
        parents.setdefault(e["to"], []).append(e["from"])

    roots = [n["id"] for n in nodes if n["id"] not in parents]
    ordered = []
    visited = set()
    queue = list(roots)
    while queue:
        nid = queue.pop(0)
        if nid in visited:
            continue
        deps = parents.get(nid, [])
        if not all(d in visited for d in deps):
            queue.append(nid)
            continue
        visited.add(nid)
        ordered.append(node_map[nid])
        for c in children.get(nid, []):
            queue.append(c)

    table = "vacancies"
    wheres: list[str] = []
    params: dict = {}
    group_cols: list[str] = []
    agg_exprs: list[str] = []
    order_parts: list[str] = []
    limit_val = 1000
    output_type = "table"
    output_config: dict = {}
    date_trunc_field = None
    pi = [0]

    def _p(val):
        pi[0] += 1
        name = f"p{pi[0]}"
        params[name] = val
        return f":{name}"

    for node in ordered:
        t = node["type"]
        c = node.get("config", {})

        if t == "vacancies_source":
            table = "vacancies"
        elif t == "companies_source":
            table = "companies"

        elif t == "filter_role":
            vals = c.get("values", [])
            mode = c.get("mode", "include")
            if vals:
                phs = ", ".join(_p(v) for v in vals)
                op = "IN" if mode == "include" else "NOT IN"
                wheres.append(f"role {op} ({phs})")

        elif t == "filter_domain":
            vals = c.get("values", [])
            if vals:
                phs = ", ".join(_p(v) for v in vals)
                wheres.append(f"domains && ARRAY[{phs}]::text[]")

        elif t == "filter_seniority":
            vals = c.get("values", [])
            mode = c.get("mode", "include")
            if vals:
                phs = ", ".join(_p(v) for v in vals)
                op = "IN" if mode == "include" else "NOT IN"
                wheres.append(f"seniority {op} ({phs})")

        elif t == "filter_salary":
            mn = c.get("min")
            mx = c.get("max")
            require = c.get("require_salary", True)
            if require:
                wheres.append("salary_min_usd IS NOT NULL OR salary_max_usd IS NOT NULL")
            if mn is not None:
                wheres.append(f"COALESCE(salary_max_usd, salary_min_usd, 0) >= {_p(int(mn))}")
            if mx is not None:
                wheres.append(f"COALESCE(salary_min_usd, salary_max_usd, 999999999) <= {_p(int(mx))}")

        elif t == "filter_location":
            vals = c.get("values", [])
            if vals:
                phs = ", ".join(_p(v) for v in vals)
                wheres.append(f"location_type IN ({phs})")

        elif t == "filter_employment":
            vals = c.get("values", [])
            if vals:
                phs = ", ".join(_p(v) for v in vals)
                wheres.append(f"metadata->>'employment_type' IN ({phs})")

        elif t == "filter_source":
            vals = c.get("values", [])
            if vals:
                phs = ", ".join(_p(v) for v in vals)
                wheres.append(f"COALESCE(source_channel, tg_channel_username) IN ({phs})")

        elif t == "filter_skill":
            vals = c.get("values", [])
            if vals:
                phs = ", ".join(_p(v) for v in vals)
                wheres.append(f"stack && ARRAY[{phs}]::text[]")

        elif t == "filter_risk":
            vals = c.get("values", [])
            mode = c.get("mode", "include")
            if vals:
                phs = ", ".join(_p(v) for v in vals)
                op = "IN" if mode == "include" else "NOT IN"
                wheres.append(f"COALESCE(risk_label, 'none') {op} ({phs})")

        elif t == "filter_score":
            mn = c.get("min")
            mx = c.get("max")
            if mn is not None:
                wheres.append(f"COALESCE(ai_score_value, 0) >= {_p(int(mn))}")
            if mx is not None:
                wheres.append(f"COALESCE(ai_score_value, 10) <= {_p(int(mx))}")

        elif t == "filter_date_range":
            preset = c.get("preset")
            if preset:
                days = {"7d": 7, "30d": 30, "90d": 90, "180d": 180, "365d": 365}.get(preset)
                if days:
                    wheres.append(f"created_at >= NOW() - INTERVAL '{days} days'")
            else:
                d_from = c.get("from")
                d_to = c.get("to")
                if d_from:
                    wheres.append(f"created_at >= {_p(d_from)}::timestamp")
                if d_to:
                    wheres.append(f"created_at <= {_p(d_to)}::timestamp")

        elif t == "filter_company":
            vals = c.get("values", [])
            mode = c.get("mode", "include")
            if vals:
                phs = ", ".join(_p(v) for v in vals)
                op = "IN" if mode == "include" else "NOT IN"
                wheres.append(f"company_name {op} ({phs})")

        elif t == "group_by":
            field = c.get("field", "company_name")
            trunc = c.get("date_trunc")
            if trunc and trunc in _STUDIO_DATE_TRUNC and field == "created_at":
                date_trunc_field = trunc
                group_cols.append(f"date_trunc('{trunc}', created_at)")
            else:
                _validate_field(field, table)
                group_cols.append(field)

        elif t == "aggregate":
            funcs = c.get("functions", [])
            for fn_def in funcs:
                fn = fn_def.get("fn", "count").lower()
                fld = fn_def.get("field", "id")
                if fn not in _STUDIO_ALLOWED_AGG:
                    raise HTTPException(400, f"Aggregate '{fn}' not allowed")
                if fld != "*":
                    _validate_field(fld, table)
                alias = fn_def.get("alias", f"{fn}_{fld}")
                alias = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in alias)
                if fn == "count" and fld == "id":
                    agg_exprs.append(f"COUNT(*) AS {alias}")
                else:
                    agg_exprs.append(f"{fn.upper()}({fld}) AS {alias}")

        elif t == "sort":
            field = c.get("field", "")
            direction = "DESC" if c.get("direction", "desc").lower() == "desc" else "ASC"
            if field:
                safe = "".join(ch for ch in field if ch.isalnum() or ch == "_")
                if not safe:
                    continue
                known_aliases = {expr.split(" AS ")[-1] for expr in agg_exprs if " AS " in expr}
                known_group = set(group_cols) | {"period"}
                all_known = known_aliases | known_group | _STUDIO_ALLOWED_FIELDS_VAC | _STUDIO_ALLOWED_FIELDS_COMP
                if safe not in all_known:
                    raise HTTPException(400, f"Sort field '{safe}' is not a known column. Available: {', '.join(sorted(known_aliases | known_group)) or 'raw table columns'}")
                order_parts.append(f"{safe} {direction}")

        elif t == "limit":
            limit_val = min(max(int(c.get("value", 1000)), 1), 10000)

        elif t in ("table_view", "chart", "export_csv"):
            output_type = t
            output_config = c

    if group_cols or agg_exprs:
        select_parts = []
        for gc in group_cols:
            if "date_trunc" in gc:
                select_parts.append(f"{gc} AS period")
            else:
                select_parts.append(gc)
        select_parts.extend(agg_exprs if agg_exprs else ["COUNT(*) AS count"])
        select_str = ", ".join(select_parts)
        group_str = " GROUP BY " + ", ".join(group_cols)
    else:
        default_cols = {
            "vacancies": "id, company_name, title, role, seniority, salary_min_usd, salary_max_usd, location_type, ai_score_value, created_at",
            "companies": "id, name, industry, size, headquarters, created_at",
        }
        select_str = default_cols.get(table, "*")
        group_str = ""

    where_str = (" WHERE " + " AND ".join(f"({w})" for w in wheres)) if wheres else ""
    order_str = ""
    if order_parts:
        order_str = " ORDER BY " + ", ".join(order_parts)
    elif agg_exprs:
        first_alias = agg_exprs[0].split(" AS ")[-1]
        order_str = f" ORDER BY {first_alias} DESC"
    elif not group_cols:
        order_str = " ORDER BY created_at DESC"

    sql = f"SELECT {select_str} FROM {table}{where_str}{group_str}{order_str} LIMIT {limit_val}"

    try:
        async with SessionLocal() as s:
            result = await s.execute(text(sql), params)
            columns = list(result.keys())
            rows = []
            for r in result.all():
                row = []
                for val in r:
                    if isinstance(val, datetime):
                        row.append(val.isoformat())
                    elif val is None:
                        row.append(None)
                    else:
                        try:
                            row.append(float(val) if isinstance(val, (int, float)) else str(val))
                        except (TypeError, ValueError):
                            row.append(str(val))
                rows.append(row)
    except Exception as exc:
        err_str = str(exc)
        if "does not exist" in err_str and "column" in err_str:
            import re
            m = re.search(r'column "([^"]+)"', err_str)
            col = m.group(1) if m else "unknown"
            hint = f"Column '{col}' not found. Make sure Sort field matches an Aggregate alias or Group By field."
            if known_aliases := {expr.split(' AS ')[-1] for expr in agg_exprs if ' AS ' in expr}:
                hint += f" Available aliases: {', '.join(sorted(known_aliases))}"
            raise HTTPException(400, hint)
        elif "syntax error" in err_str.lower():
            raise HTTPException(400, "SQL syntax error in pipeline. Try simplifying your pipeline.")
        else:
            raise HTTPException(400, f"Query error: {exc}")

    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "output_type": output_type,
        "output_config": output_config,
        "sql_preview": sql,
    }


@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page(
  request: Request,
  domains: str | None = Query(None),
  roles: str | None = Query(None),
  locations: str | None = Query(None),
  seniorities: str | None = Query(None),
  sources: str | None = Query(None),
  score_min: int | None = Query(None),
  score_max: int | None = Query(None),
  companies: str | None = Query(None),
  skills: str | None = Query(None),
  salary_min: int | None = Query(None),
  salary_max: int | None = Query(None),
):
  category_stats: list[tuple] = []
  channel_stats: list[tuple] = []
  date_stats: list[tuple] = []
  salary_stats: list[tuple] = []
  seniority_stats: list[tuple] = []
  role_stats: list[tuple] = []
  location_stats: list[tuple] = []
  employment_stats: list[tuple] = []
  title_stats: list[tuple] = []
  risk_stats: list[tuple] = []
  score_stats: list[tuple] = []
  total_vac = total_ch = last_7 = with_salary = high_risk = with_company = 0
  charts_json = _charts_json_for_template(_empty_analytics_charts())

  async with SessionLocal() as s:
    try:
      vc = await _pg_table_columns(s, "vacancies")
    except Exception:
      vc = set()

    # ── Parse filters ──
    f_domains = [d.strip().lower() for d in (domains or "").split(",") if d.strip()]
    f_roles = [r.strip() for r in (roles or "").split(",") if r.strip()]
    f_locations = [l.strip().lower() for l in (locations or "").split(",") if l.strip()]
    f_seniorities = [s_.strip().lower() for s_ in (seniorities or "").split(",") if s_.strip()]
    f_sources = [sc.strip() for sc in (sources or "").split(",") if sc.strip()]
    f_companies = [c.strip() for c in (companies or "").split(",") if c.strip()]
    f_skills = [sk.strip().lower() for sk in (skills or "").split(",") if sk.strip()]

    def _build_where(prefix="vacancies"):
      clauses = []
      if f_domains:
        arr = ",".join(f"\'{d}\'" for d in f_domains)
        clauses.append(f"{prefix}.domains && ARRAY[{arr}]::text[]")
      if f_roles:
        arr = ",".join(f"\'{r}\'" for r in f_roles)
        clauses.append(f"lower(btrim({prefix}.role)) IN ({arr})")
      if f_locations:
        arr = ",".join(f"\'{l}\'" for l in f_locations)
        clauses.append(f"lower(btrim({prefix}.location_type)) IN ({arr})")
      if f_seniorities:
        arr = ",".join(f"\'{s_}\'" for s_ in f_seniorities)
        clauses.append(f"lower(btrim({prefix}.seniority)) IN ({arr})")
      if f_sources:
        arr = ",".join(f"\'{sc}\'" for sc in f_sources)
        clauses.append(f"btrim({prefix}.source_channel) IN ({arr})")
      if score_min is not None:
        clauses.append(f"{prefix}.ai_score_value >= {int(score_min)}")
      if score_max is not None:
        clauses.append(f"{prefix}.ai_score_value <= {int(score_max)}")
      if f_companies:
        arr = ",".join(f"\'{c}\'" for c in f_companies)
        clauses.append(f"btrim({prefix}.company_name) IN ({arr})")
      if f_skills:
        arr = ",".join(f"\'{sk}\'" for sk in f_skills)
        clauses.append(f"{prefix}.stack && ARRAY[{arr}]::text[]")
      if salary_min is not None:
        clauses.append(f"COALESCE({prefix}.salary_min_usd, {prefix}.salary_max_usd, 0) >= {int(salary_min)}")
      if salary_max is not None:
        clauses.append(f"COALESCE({prefix}.salary_max_usd, {prefix}.salary_min_usd, 999999999) <= {int(salary_max)}")
      return (" AND " + " AND ".join(clauses)) if clauses else ""

    _wh = _build_where()
    _wh_v = _build_where("v")

    _all_domains_rows = await _exec_text_all(s, "SELECT DISTINCT lower(btrim(d::text)) AS d FROM vacancies CROSS JOIN LATERAL unnest(domains) AS d WHERE d IS NOT NULL AND btrim(d::text) <> '' ORDER BY 1") if "domains" in vc else []
    _all_roles_rows = await _exec_text_all(s, "SELECT DISTINCT btrim(role) AS r FROM vacancies WHERE role IS NOT NULL AND btrim(role) <> '' ORDER BY 1") if "role" in vc else []
    _all_locations_rows = await _exec_text_all(s, "SELECT DISTINCT lower(btrim(location_type)) AS l FROM vacancies WHERE location_type IS NOT NULL AND btrim(location_type) <> '' ORDER BY 1")
    _all_seniorities_rows = await _exec_text_all(s, "SELECT DISTINCT lower(btrim(seniority)) AS s FROM vacancies WHERE seniority IS NOT NULL AND btrim(seniority) <> '' ORDER BY 1") if "seniority" in vc else []
    _all_sources_rows = await _exec_text_all(s, "SELECT DISTINCT btrim(source_channel) AS sc FROM vacancies WHERE source_channel IS NOT NULL AND btrim(source_channel) <> '' ORDER BY 1") if "source_channel" in vc else []
    _all_companies_rows = await _exec_text_all(s, "SELECT btrim(company_name) AS c, COUNT(*) AS cnt FROM vacancies WHERE company_name IS NOT NULL AND btrim(company_name) <> '' GROUP BY 1 ORDER BY cnt DESC LIMIT 200")
    _all_skills_rows = await _exec_text_all(s, "SELECT lower(btrim(sk::text)) AS sk, COUNT(*) AS cnt FROM vacancies CROSS JOIN LATERAL unnest(stack) AS sk WHERE sk IS NOT NULL AND btrim(sk::text) <> '' GROUP BY 1 ORDER BY cnt DESC LIMIT 150") if "stack" in vc else []
    filter_options = {"domains": [r.d for r in _all_domains_rows], "roles": [r.r for r in _all_roles_rows], "locations": [r.l for r in _all_locations_rows], "seniorities": [r.s for r in _all_seniorities_rows], "sources": [r.sc for r in _all_sources_rows], "companies": [r.c for r in _all_companies_rows], "skills": [r.sk for r in _all_skills_rows]}
    active_filters = {"domains": f_domains, "roles": f_roles, "locations": f_locations, "seniorities": f_seniorities, "sources": f_sources, "score_min": score_min, "score_max": score_max, "companies": f_companies, "skills": f_skills, "salary_min": salary_min, "salary_max": salary_max}

    total_vac = (await s.execute(text(f"SELECT COUNT(*) FROM vacancies WHERE 1=1 {_wh}"))).scalar() or 0
    _tg_ch = (await s.execute(select(func.count(Channel.id)))).scalar() or 0
    _ws_ch = 0
    try:
      _ws_ch = (await s.execute(select(func.count(WebSource.id)))).scalar() or 0
    except Exception:
      pass
    total_ch = _tg_ch + _ws_ch
    _cut7 = datetime.now(timezone.utc) - timedelta(days=7)
    last_7 = (await s.execute(text(f"SELECT COUNT(*) FROM vacancies WHERE created_at >= '{_cut7.isoformat()}' {_wh}"))).scalar() or 0
    with_salary = (await s.execute(text(f"SELECT COUNT(*) FROM vacancies WHERE (salary_min_usd IS NOT NULL OR salary_max_usd IS NOT NULL) {_wh}"))).scalar() or 0

    if "risk_label" in vc:
      try:
        high_risk = (await s.execute(select(func.count(Vacancy.id)).where(Vacancy.risk_label == "high-risk"))).scalar() or 0
      except Exception:
        high_risk = 0
    if "company_id" in vc:
      try:
        with_company = (await s.execute(select(func.count(Vacancy.id)).where(Vacancy.company_id.isnot(None)))).scalar() or 0
      except Exception:
        with_company = 0

    try:
      if "domains" in vc:
        category_stats_rows = await _exec_text_all(
          s,
          f"""
          SELECT lower(btrim(d::text)) AS category, COUNT(*) AS count
          FROM vacancies v
          CROSS JOIN LATERAL unnest(v.domains) AS d
          WHERE d IS NOT NULL AND btrim(d::text) <> '' {_wh_v}
          GROUP BY 1
          ORDER BY count DESC
          """,
        )
        category_stats = [(r.category, r.count) for r in category_stats_rows]
      elif "domain" in vc:
        category_stats_rows = await _exec_text_all(
          s,
          """
          SELECT lower(btrim(domain::text)) AS category, COUNT(*) AS count
          FROM vacancies v
          WHERE domain IS NOT NULL AND btrim(domain::text) <> ''
          GROUP BY 1
          ORDER BY count DESC
          """,
        )
        category_stats = [(r.category, r.count) for r in category_stats_rows]
      elif "category" in vc:
        category_stats_rows = await _exec_text_all(
          s,
          """
          SELECT lower(btrim(category::text)) AS category, COUNT(*) AS count
          FROM vacancies v
          WHERE category IS NOT NULL AND btrim(category::text) <> ''
          GROUP BY 1
          ORDER BY count DESC
          """,
        )
        category_stats = [(r.category, r.count) for r in category_stats_rows]
    except Exception:
      category_stats = []

    try:
      if "source_channel" in vc:
        source_stats_rows = await _exec_text_all(
          s,
          f"""
          SELECT CASE
                   WHEN btrim(source_channel) LIKE 'web:%%' THEN '🌐 ' || btrim(source_channel)
                   WHEN btrim(tg_channel_username) <> '' THEN '@' || btrim(tg_channel_username)
                   WHEN btrim(source_channel) <> '' THEN btrim(source_channel)
                   ELSE '(unknown)'
                 END AS src,
                 COUNT(*) AS count
          FROM vacancies WHERE 1=1 {_wh}
          GROUP BY 1
          ORDER BY count DESC
          LIMIT 25
          """,
        )
      else:
        source_stats_rows = await _exec_text_all(
          s,
          """
          SELECT COALESCE('@' || NULLIF(btrim(tg_channel_username), ''), '(unknown)') AS src,
                 COUNT(*) AS count
          FROM vacancies
          GROUP BY 1
          ORDER BY count DESC
          LIMIT 25
          """,
        )
      channel_stats = [(r.src, r.count) for r in source_stats_rows]
    except Exception:
      channel_stats = []

    try:
      date_stats_rows = (
        await s.execute(
          select(
            func.date(Vacancy.created_at).label("date"),
            func.count(Vacancy.id).label("count"),
          )
          .where(text(f"vacancies.created_at >= (CURRENT_DATE - INTERVAL '30 days') {_wh}"))
          .group_by(func.date(Vacancy.created_at))
          .order_by(func.date(Vacancy.created_at).asc())
        )
      ).all()
      date_stats = [(r.date, r.count) for r in date_stats_rows]
    except Exception:
      date_stats = []

    avg_score_stats: list[tuple] = []
    try:
      avg_score_rows = await _exec_text_all(s, f"""
        SELECT date(created_at) AS d, ROUND(AVG(ai_score_value)::numeric, 1) AS avg_score,
               COUNT(*) AS cnt
        FROM vacancies
        WHERE created_at >= (CURRENT_DATE - INTERVAL '30 days')
          AND ai_score_value IS NOT NULL {_wh}
        GROUP BY date(created_at)
        ORDER BY d
      """)
      avg_score_stats = [(r.d, float(r.avg_score), int(r.cnt)) for r in avg_score_rows]
    except Exception:
      avg_score_stats = []

    try:
      if "domains" in vc:
        salary_stats_rows = await _exec_text_all(
          s,
          f"""
          SELECT
            lower(btrim(d::text)) AS category,
            AVG(v.salary_min_usd) AS avg_min,
            AVG(v.salary_max_usd) AS avg_max,
            COUNT(v.id) AS count
          FROM vacancies v
          CROSS JOIN LATERAL unnest(v.domains) AS d
          WHERE d IS NOT NULL AND btrim(d::text) <> ''
            AND (v.salary_min_usd IS NOT NULL OR v.salary_max_usd IS NOT NULL) {_wh_v}
          GROUP BY 1
          ORDER BY count DESC
          """,
        )
        salary_stats = [(r.category, r.avg_min, r.avg_max, r.count) for r in salary_stats_rows]
      elif "domain" in vc:
        salary_stats_rows = await _exec_text_all(
          s,
          """
          SELECT
            lower(btrim(domain::text)) AS category,
            AVG(salary_min_usd) AS avg_min,
            AVG(salary_max_usd) AS avg_max,
            COUNT(id) AS count
          FROM vacancies v
          WHERE domain IS NOT NULL AND btrim(domain::text) <> ''
            AND (salary_min_usd IS NOT NULL OR salary_max_usd IS NOT NULL)
          GROUP BY 1
          ORDER BY count DESC
          """,
        )
        salary_stats = [(r.category, r.avg_min, r.avg_max, r.count) for r in salary_stats_rows]
    except Exception:
      salary_stats = []

    salary_by_role: list[tuple] = []
    if "role" in vc:
      try:
        sbr_rows = await _exec_text_all(
          s,
          """
          SELECT role,
                 ROUND(AVG(salary_min_usd))::int AS avg_min,
                 ROUND(AVG(salary_max_usd))::int AS avg_max,
                 COUNT(*) AS count
          FROM vacancies
          WHERE role IS NOT NULL AND role <> ''
            AND salary_min_usd IS NOT NULL
          GROUP BY role
          HAVING COUNT(*) >= 2
          ORDER BY avg_max DESC
          LIMIT 15
          """,
        )
        salary_by_role = [(r.role, r.avg_min, r.avg_max, r.count) for r in sbr_rows]
      except Exception:
        salary_by_role = []

    if "seniority" in vc:
      try:
        seniority_rows = await _exec_text_all(
          s,
          f"""
          SELECT lower(btrim(seniority)) AS sen, COUNT(*) AS count
          FROM vacancies
          WHERE seniority IS NOT NULL AND btrim(seniority) <> '' {_wh}
          GROUP BY 1
          ORDER BY count DESC
          LIMIT 16
          """,
        )
        seniority_stats = [(r.sen, r.count) for r in seniority_rows]
      except Exception:
        seniority_stats = []

    if "role" in vc:
      try:
        role_rows = await _exec_text_all(
          s,
          f"""
          SELECT btrim(role) AS r, COUNT(*) AS count
          FROM vacancies
          WHERE role IS NOT NULL AND btrim(role) <> '' {_wh}
          GROUP BY 1
          ORDER BY count DESC
          LIMIT 20
          """,
        )
        role_stats = [(r.r, r.count) for r in role_rows]
      except Exception:
        role_stats = []

    try:
      location_rows = (
        await s.execute(
          select(Vacancy.location_type, func.count(Vacancy.id).label("count"))
          .where(Vacancy.location_type.isnot(None))
          .where(Vacancy.location_type != "")
          .group_by(Vacancy.location_type)
          .order_by(desc("count"))
        )
      ).all()
      location_stats = [(r.location_type, r.count) for r in location_rows]
    except Exception:
      location_stats = []

    meta_col = "metadata_json" if "metadata_json" in vc else "metadata" if "metadata" in vc else None
    if meta_col:
      try:
        employment_rows = await _exec_text_all(
          s,
          f"""
          SELECT lower(btrim({meta_col}->>'employment_type')) AS et, COUNT(*) AS count
          FROM vacancies
          WHERE {meta_col} ? 'employment_type'
            AND btrim(COALESCE({meta_col}->>'employment_type', '')) <> ''
          GROUP BY 1
          ORDER BY count DESC
          LIMIT 12
          """,
        )
        employment_stats = [(r.et, r.count) for r in employment_rows]
      except Exception:
        employment_stats = []

    try:
      if "title" not in vc:
        title_stats = []
      elif "standardized_title" in vc:
        title_rows = await _exec_text_all(
          s,
          """
          SELECT COALESCE(NULLIF(btrim(standardized_title), ''), left(btrim(COALESCE(title, '')), 56)) AS job_label,
                 COUNT(*) AS c
          FROM vacancies
          WHERE COALESCE(NULLIF(btrim(standardized_title), ''), btrim(COALESCE(title, ''))) <> ''
          GROUP BY 1
          ORDER BY c DESC
          LIMIT 18
          """,
        )
        title_stats = [(r.job_label, r.c) for r in title_rows]
      else:
        title_rows = await _exec_text_all(
          s,
          """
          SELECT left(btrim(COALESCE(title, '')), 56) AS job_label, COUNT(*) AS c
          FROM vacancies
          WHERE btrim(COALESCE(title, '')) <> ''
          GROUP BY 1
          ORDER BY c DESC
          LIMIT 18
          """,
        )
        title_stats = [(r.job_label, r.c) for r in title_rows]
    except Exception:
      title_stats = []

    if "risk_label" in vc:
      try:
        risk_rows = await _exec_text_all(
          s,
          """
          SELECT COALESCE(NULLIF(btrim(risk_label), ''), '(none)') AS r, COUNT(*) AS count
          FROM vacancies
          GROUP BY 1
          ORDER BY count DESC
          """,
        )
        risk_stats = [(r.r, r.count) for r in risk_rows]
      except Exception:
        risk_stats = []

    if meta_col:
      try:
        score_rows = await _exec_text_all(
          s,
          f"""
          SELECT bucket_id, bucket_label, COUNT(*) AS count
          FROM (
            SELECT
              CASE
                WHEN sc IS NULL THEN 0
                WHEN sc < 3 THEN 1
                WHEN sc < 5 THEN 2
                WHEN sc < 7 THEN 3
                WHEN sc < 9 THEN 4
                ELSE 5
              END AS bucket_id,
              CASE
                WHEN sc IS NULL THEN 'No score'
                WHEN sc < 3 THEN '0 – 2.9'
                WHEN sc < 5 THEN '3 – 4.9'
                WHEN sc < 7 THEN '5 – 6.9'
                WHEN sc < 9 THEN '7 – 8.9'
                ELSE '9 – 10'
              END AS bucket_label
            FROM (
              SELECT
                CASE
                  WHEN ({meta_col}->'scoring'->>'total_score') ~ '^[0-9]+(\\.[0-9]+)?$'
                    THEN ({meta_col}->'scoring'->>'total_score')::double precision
                  WHEN ai_score_value IS NOT NULL THEN ai_score_value::double precision
                  ELSE NULL
                END AS sc
              FROM vacancies
            ) s
          ) b
          GROUP BY bucket_id, bucket_label
          ORDER BY bucket_id
          """,
        )
        score_stats = [(r.bucket_label, r.count) for r in score_rows]
      except Exception:
        score_stats = []

    lang_req_stats: list[tuple] = []
    try:
      lr_rows = await _exec_text_all(s, f"""
        SELECT initcap(kv.key) AS lang, COUNT(*) AS cnt
        FROM vacancies,
             jsonb_each_text(metadata->'language_requirements') AS kv
        WHERE jsonb_typeof(metadata->'language_requirements') = 'object' {_wh}
        GROUP BY kv.key
        ORDER BY cnt DESC
        LIMIT 20
      """)
      lang_req_stats = [(r.lang, r.cnt) for r in lr_rows]
    except Exception:
      lang_req_stats = []

    top_companies_stats: list[tuple] = []
    try:
      tc_rows = await _exec_text_all(s, f"""
        SELECT COALESCE(NULLIF(btrim(company_name), ''), '(unknown)') AS cname,
               COUNT(*) AS cnt
        FROM vacancies
        WHERE company_name IS NOT NULL AND btrim(company_name) <> '' {_wh}
        GROUP BY 1
        ORDER BY cnt DESC
        LIMIT 15
      """)
      top_companies_stats = [(r.cname, r.cnt) for r in tc_rows]
    except Exception:
      top_companies_stats = []

    def _fmt_chart_date(d):
      if d is None:
        return ""
      if hasattr(d, "strftime"):
        return d.strftime("%Y-%m-%d")
      return str(d)

    def _safe_chart_float(x: object) -> float:
      try:
        v = float(x) if x is not None else 0.0
      except (TypeError, ValueError):
        return 0.0
      return v if math.isfinite(v) else 0.0

    _salary_mid = [
      _safe_chart_float((mn or 0) + (mx or 0)) / 2.0
      if mn is not None and mx is not None
      else _safe_chart_float(mn if mn is not None else mx)
      for _, mn, mx, _ in salary_stats
    ]

    charts = {
      "avg_score_timeline": {
        "labels": [_fmt_chart_date(d) for d, _, _ in avg_score_stats],
        "scores": [s for _, s, _ in avg_score_stats],
        "counts": [c for _, _, c in avg_score_stats],
      },
      "domains": {
        "labels": [str(c) if c is not None else "" for c, _ in category_stats],
        "counts": [_safe_int_count(n) for _, n in category_stats],
      },
      "timeline": {"labels": [_fmt_chart_date(d) for d, _ in date_stats], "counts": [_safe_int_count(n) for _, n in date_stats]},
      "sources": {"labels": [str(s) for s, _ in channel_stats], "counts": [_safe_int_count(n) for _, n in channel_stats]},
      "seniority": {"labels": [str(x) for x, _ in seniority_stats], "counts": [_safe_int_count(n) for _, n in seniority_stats]},
      "roles": {"labels": [str(x) for x, _ in role_stats], "counts": [_safe_int_count(n) for _, n in role_stats]},
      "location": {"labels": [str(x) for x, _ in location_stats], "counts": [_safe_int_count(n) for _, n in location_stats]},
      "employment": {"labels": [str(x) for x, _ in employment_stats], "counts": [_safe_int_count(n) for _, n in employment_stats]},
      "titles": {"labels": [str(t) for t, _ in title_stats], "counts": [_safe_int_count(n) for _, n in title_stats]},
      "risk": {"labels": [str(x) for x, _ in risk_stats], "counts": [_safe_int_count(n) for _, n in risk_stats]},
      "scores": {"labels": [str(x) for x, _ in score_stats], "counts": [_safe_int_count(n) for _, n in score_stats]},
      "required_languages": {"labels": [str(x) for x, _ in lang_req_stats], "counts": [_safe_int_count(n) for _, n in lang_req_stats]},
      "top_companies": {"labels": [str(c) for c, _ in top_companies_stats], "counts": [_safe_int_count(n) for _, n in top_companies_stats]},
      "salary_domains": {
        "labels": [str(c) for c, _, _, _ in salary_stats],
        "avg_mid": _salary_mid,
      },
      "salary_roles": {
        "labels": [str(r) for r, _, _, _ in salary_by_role],
        "avg_min": [_safe_chart_float(mn) for _, mn, _, _ in salary_by_role],
        "avg_max": [_safe_chart_float(mx) for _, _, mx, _ in salary_by_role],
      },
    }

    charts_json = _charts_json_for_template(charts)

  return templates.TemplateResponse(
    request, "analytics.html",
    {
      "charts_json": charts_json,
      "kpis": {
        "total_vacancies": total_vac,
        "total_channels": total_ch,
        "last_7_days": last_7,
        "with_salary": with_salary,
        "high_risk": high_risk,
        "with_company": with_company,
      },
      "category_stats": category_stats,
      "channel_stats": channel_stats,
      "date_stats": date_stats,
      "salary_stats": salary_stats,
      "seniority_stats": seniority_stats,
      "role_stats": role_stats,
      "salary_by_role": salary_by_role,
      "location_stats": location_stats,
      "employment_stats": employment_stats,
      "title_stats": title_stats,
      "risk_stats": risk_stats,
      "score_stats": score_stats,
      "lang_req_stats": lang_req_stats,
      "charts": charts,
      "filter_options": filter_options,
      "active_filters": active_filters,
    },
  )


@app.get("/graph", response_class=HTMLResponse)
async def graph_page(request: Request):
  return templates.TemplateResponse(request, "graph.html")


@app.get("/api/graph/vacancies")
async def graph_vacancies_data():
  """Return lightweight vacancy records for the force-directed graph."""
  async with SessionLocal() as s:
    rows = (await s.execute(
      select(
        Vacancy.id,
        Vacancy.title,
        Vacancy.company_name,
        Vacancy.ai_score_value,
        Vacancy.salary_min_usd,
        Vacancy.salary_max_usd,
        Vacancy.domains,
        Vacancy.role,
        Vacancy.seniority,
        Vacancy.location_type,
        Vacancy.source_channel,
      ).order_by(desc(Vacancy.created_at))
    )).all()

  nodes = []
  for r in rows:
    domains = list(r.domains or [])
    primary_domain = domains[0] if domains else "other"
    sal_avg = None
    if r.salary_min_usd and r.salary_max_usd:
      sal_avg = (r.salary_min_usd + r.salary_max_usd) // 2
    elif r.salary_max_usd:
      sal_avg = r.salary_max_usd
    elif r.salary_min_usd:
      sal_avg = r.salary_min_usd
    nodes.append({
      "id": r.id,
      "title": r.title or "",
      "company": r.company_name or "",
      "score": r.ai_score_value,
      "salary": sal_avg,
      "domain": primary_domain,
      "domains": domains,
      "role": r.role,
      "seniority": r.seniority,
      "location": r.location_type,
      "source": r.source_channel,
    })

  return {"nodes": nodes}


# ─── Single vacancy SEO page ─────────────────────────────────────────────────

@app.get("/vacancies/{slug_and_id:path}", response_class=HTMLResponse)
async def vacancy_detail_page(request: Request, slug_and_id: str):
  """SEO-friendly vacancy page: /vacancies/senior-dev-at-binance-1234"""
  # Extract numeric ID from the end of the slug
  m = _re_mod.search(r"(\d+)$", slug_and_id.rstrip("/"))
  if not m:
    return RedirectResponse(url="/vacancies", status_code=302)
  vacancy_id = int(m.group(1))

  async with SessionLocal() as s:
    row = (
      await s.execute(
        select(Vacancy, Company.logo_url, Company.website.label("cw"),
               Company.industry, Company.summary.label("cs"))
        .select_from(Vacancy)
        .outerjoin(Company, Company.id == Vacancy.company_id)
        .where(Vacancy.id == vacancy_id)
      )
    ).one_or_none()
    if not row:
      return RedirectResponse(url="/vacancies", status_code=302)

    v = row[0]
    logo_url = row[1]
    meta = getattr(v, "metadata_json", {}) or {}
    scoring = meta.get("scoring") or {}
    total_score = scoring.get("total_score") or getattr(v, "ai_score_value", None)

    canonical_slug = _vacancy_slug(v.title, v.company_name)
    canonical_url = f"https://hirelens.xyz/vacancies/{canonical_slug}-{v.id}"

    # Redirect to canonical URL if slug doesn't match
    expected_path = f"/vacancies/{canonical_slug}-{v.id}"
    actual_path = f"/vacancies/{slug_and_id}"
    if actual_path != expected_path:
      return RedirectResponse(url=expected_path, status_code=301)

    description = (getattr(v, "summary_en", None) or getattr(v, "description", None) or v.raw_text or "")[:200].replace("\n", " ").strip()
    salary_text = ""
    if v.salary_min_usd or v.salary_max_usd:
      salary_text = f" | ${v.salary_min_usd or '?'}–${v.salary_max_usd or '?'}"

    seo = {
      "title": f"{v.title or 'Vacancy'} at {v.company_name or 'Company'} | HireLens",
      "description": description + salary_text,
      "canonical": canonical_url,
      "og_type": "article",
      "logo_url": logo_url,
      "company_name": v.company_name,
      "vacancy_title": v.title,
      "location_type": v.location_type,
      "salary_min": v.salary_min_usd,
      "salary_max": v.salary_max_usd,
      "created_at": v.created_at.isoformat() if v.created_at else None,
      "domains": getattr(v, "domains", []) or [],
      "stack": getattr(v, "stack", []) or [],
      "seniority": getattr(v, "seniority", None),
    }

    return templates.TemplateResponse(
      request, "vacancy_detail.html",
      {"vacancy_id": vacancy_id, "seo": seo},
    )


@app.get("/vacancy/{slug_and_id:path}", response_class=HTMLResponse)
async def vacancy_fullpage(request: Request, slug_and_id: str):
  """Standalone full-page vacancy view: /vacancy/senior-dev-at-binance-1234"""
  m = _re_mod.search(r"(\d+)$", slug_and_id.rstrip("/"))
  if not m:
    return RedirectResponse(url="/vacancies", status_code=302)
  vacancy_id = int(m.group(1))

  async with SessionLocal() as s:
    row = (
      await s.execute(
        select(Vacancy, Company.logo_url)
        .select_from(Vacancy)
        .outerjoin(Company, Company.id == Vacancy.company_id)
        .where(Vacancy.id == vacancy_id)
      )
    ).one_or_none()
    if not row:
      return RedirectResponse(url="/vacancies", status_code=302)

    v = row[0]
    logo_url = row[1]

    canonical_slug = _vacancy_slug(v.title, v.company_name)
    canonical_url = f"https://hirelens.xyz/vacancy/{canonical_slug}-{v.id}"

    expected_path = f"/vacancy/{canonical_slug}-{v.id}"
    actual_path = f"/vacancy/{slug_and_id}"
    if actual_path != expected_path:
      return RedirectResponse(url=expected_path, status_code=301)

    description = (getattr(v, "summary_en", None) or getattr(v, "description", None) or v.raw_text or "")[:200].replace("\n", " ").strip()
    salary_text = ""
    if v.salary_min_usd or v.salary_max_usd:
      salary_text = f" | ${v.salary_min_usd or '?'}–${v.salary_max_usd or '?'}"

    seo = {
      "title": f"{v.title or 'Vacancy'} at {v.company_name or 'Company'} | HireLens",
      "description": description + salary_text,
      "canonical": canonical_url,
      "logo_url": logo_url,
      "company_name": v.company_name,
      "vacancy_title": v.title,
      "location_type": v.location_type,
      "salary_min": v.salary_min_usd,
      "salary_max": v.salary_max_usd,
      "created_at": v.created_at.isoformat() if v.created_at else None,
      "domains": getattr(v, "domains", []) or [],
      "stack": getattr(v, "stack", []) or [],
      "seniority": getattr(v, "seniority", None),
    }

    return templates.TemplateResponse(
      request, "vacancy_page.html",
      {"vacancy_id": vacancy_id, "seo": seo},
    )


# ─── Companies page ──────────────────────────────────────────────────────────

@app.get("/companies", response_class=HTMLResponse)
async def companies_page(
  request: Request,
  page: int = Query(1, ge=1),
  per_page: int = Query(60, ge=1, le=200),
  search: str | None = Query(None),
  industry: str | None = Query(None),
  sort_by: str = Query("vacancies_desc"),
):
  async with SessionLocal() as s:
    query = select(
      Company,
      func.count(Vacancy.id).label("vac_count"),
    ).outerjoin(Vacancy, Vacancy.company_id == Company.id).group_by(Company.id)

    if search and search.strip():
      query = query.where(
        or_(Company.name.ilike(f"%{search.strip()}%"), Company.industry.ilike(f"%{search.strip()}%"))
      )
    if industry and industry.strip():
      query = query.where(func.lower(Company.industry) == industry.strip().lower())

    total = (await s.execute(
      select(func.count()).select_from(query.subquery())
    )).scalar() or 0

    _sort = {
      "vacancies_desc": [desc("vac_count"), desc(Company.id)],
      "vacancies_asc": [text("vac_count ASC"), desc(Company.id)],
      "name_asc": [Company.name, desc(Company.id)],
      "name_desc": [desc(Company.name), desc(Company.id)],
      "newest": [desc(Company.created_at)],
      "oldest": [Company.created_at],
    }
    order = _sort.get(sort_by, _sort["vacancies_desc"])

    rows = (await s.execute(
      query.order_by(*order).offset((page - 1) * per_page).limit(per_page)
    )).all()

    companies = []
    for comp, vac_count in rows:
      companies.append({
        "id": comp.id,
        "name": comp.name,
        "website": comp.website,
        "linkedin": comp.linkedin,
        "logo_url": comp.logo_url,
        "summary": comp.summary,
        "industry": comp.industry,
        "size": comp.size,
        "founded": comp.founded,
        "headquarters": comp.headquarters,
        "domains": comp.domains or [],
        "socials": comp.socials or {},
        "vac_count": vac_count,
      })

    industry_rows = (await s.execute(
      select(Company.industry, func.count(Company.id))
      .where(Company.industry.isnot(None), Company.industry != "")
      .group_by(Company.industry)
      .order_by(desc(func.count(Company.id)))
    )).all()
    industries = [(r[0], r[1]) for r in industry_rows]

  return templates.TemplateResponse(request, "companies.html", {
    "companies": companies,
    "total": total,
    "page": page,
    "per_page": per_page,
    "total_pages": max(1, (total + per_page - 1) // per_page),
    "search": search,
    "industry": industry,
    "sort_by": sort_by,
    "industries": industries,
  })


@app.get("/api/companies/{company_id}")
async def api_company_detail(company_id: int):
  async with SessionLocal() as s:
    comp = (await s.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not comp:
      raise HTTPException(404, "Company not found")

    vac_rows = (await s.execute(
      select(
        Vacancy.id, Vacancy.title, Vacancy.role, Vacancy.seniority,
        Vacancy.salary_min_usd, Vacancy.salary_max_usd,
        Vacancy.ai_score_value, Vacancy.location_type, Vacancy.domains,
        Vacancy.created_at, Vacancy.source_channel,
      )
      .where(Vacancy.company_id == company_id)
      .order_by(desc(Vacancy.created_at))
      .limit(50)
    )).all()

    vacancies = []
    for r in vac_rows:
      vacancies.append({
        "id": r.id, "title": r.title, "role": r.role, "seniority": r.seniority,
        "salary_min_usd": r.salary_min_usd, "salary_max_usd": r.salary_max_usd,
        "ai_score_value": r.ai_score_value, "location_type": r.location_type,
        "domains": list(r.domains or []),
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "source_channel": r.source_channel,
      })

    return {
      "id": comp.id,
      "name": comp.name,
      "website": comp.website,
      "linkedin": comp.linkedin,
      "logo_url": comp.logo_url,
      "summary": comp.summary,
      "industry": comp.industry,
      "size": comp.size,
      "founded": comp.founded,
      "headquarters": comp.headquarters,
      "domains": comp.domains or [],
      "socials": comp.socials or {},
      "vacancies": vacancies,
    }


# ─── Single company SEO page ──────────────────────────────────────────────────

@app.get("/companies/{slug_and_id:path}", response_class=HTMLResponse)
async def company_detail_page(request: Request, slug_and_id: str):
  """SEO-friendly company page: /companies/binance-42"""
  m = _re_mod.search(r"(\d+)$", slug_and_id.rstrip("/"))
  if not m:
    return RedirectResponse(url="/companies", status_code=302)
  company_id = int(m.group(1))

  async with SessionLocal() as s:
    comp = (await s.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not comp:
      return RedirectResponse(url="/companies", status_code=302)

    vac_count = (await s.execute(
      select(func.count(Vacancy.id)).where(Vacancy.company_id == company_id)
    )).scalar() or 0

    canonical_slug = _vacancy_slug(comp.name)
    canonical_url = f"https://hirelens.xyz/companies/{canonical_slug}-{comp.id}"

    expected_path = f"/companies/{canonical_slug}-{comp.id}"
    actual_path = f"/companies/{slug_and_id}"
    if actual_path != expected_path:
      return RedirectResponse(url=expected_path, status_code=301)

    description = (comp.summary or f"{comp.name} company profile")[:200]
    meta_parts = []
    if comp.industry:
      meta_parts.append(comp.industry)
    if comp.headquarters:
      meta_parts.append(comp.headquarters)
    if vac_count:
      meta_parts.append(f"{vac_count} open positions")
    if meta_parts:
      description += " | " + ", ".join(meta_parts)

    seo = {
      "title": f"{comp.name} — Company Profile | HireLens",
      "description": description,
      "canonical": canonical_url,
      "logo_url": comp.logo_url,
      "name": comp.name,
      "website": comp.website,
      "industry": comp.industry,
      "size": comp.size,
      "founded": comp.founded,
      "headquarters": comp.headquarters,
      "summary": comp.summary,
      "domains": comp.domains or [],
      "vac_count": vac_count,
    }

    return templates.TemplateResponse(
      request, "company_detail.html",
      {"company_id": company_id, "seo": seo},
    )


# ──────────────────────── Documentation ────────────────────────

SECTION_ORDER = ["Getting Started", "Platform", "Analytics Studio", "Market Map", "API"]

def _section_key(s: str) -> int:
    try:
        return SECTION_ORDER.index(s)
    except ValueError:
        return 999


@app.get("/docs", response_class=HTMLResponse)
async def docs_index(request: Request):
    """Redirect to first article."""
    async with SessionLocal() as s:
        rows = (await s.execute(
            text("SELECT slug FROM doc_articles WHERE is_published = true ORDER BY sort_order LIMIT 1")
        )).all()
    if rows:
        return RedirectResponse(f"/docs/{rows[0][0]}", status_code=302)
    return templates.TemplateResponse(request, "docs.html", {"sections": [], "article": None, "is_admin": request.session.get("authenticated")})


@app.get("/docs/{slug}", response_class=HTMLResponse)
async def docs_article(request: Request, slug: str):
    async with SessionLocal() as s:
        all_rows = (await s.execute(
            text("SELECT id, section, title, slug, content, sort_order, is_published FROM doc_articles ORDER BY sort_order")
        )).all()

        article_row = None
        for r in all_rows:
            if r[3] == slug:
                article_row = r
                break

        if not article_row:
            raise HTTPException(404, "Article not found")

        is_admin = request.session.get("authenticated")
        published = [r for r in all_rows if r[6] or is_admin]

        sections_map: dict = {}
        for r in published:
            sec = r[1]
            if sec not in sections_map:
                sections_map[sec] = []
            sections_map[sec].append({"id": r[0], "title": r[2], "slug": r[3], "is_published": r[6]})

        sections = [{"name": s, "articles": sections_map[s]} for s in sorted(sections_map.keys(), key=_section_key)]

        article = {
            "id": article_row[0],
            "section": article_row[1],
            "title": article_row[2],
            "slug": article_row[3],
            "content": article_row[4],
            "sort_order": article_row[5],
            "is_published": article_row[6],
        }

    return templates.TemplateResponse(request, "docs.html", {
        "sections": sections,
        "article": article,
        "is_admin": is_admin,
    })


@app.post("/api/docs/articles")
async def docs_create_article(request: Request, _: bool = Depends(require_auth)):
    body = await request.json()
    section = body.get("section", "Uncategorized")
    title = body.get("title", "New Article")
    slug = body.get("slug", "")
    content = body.get("content", "")
    sort_order = int(body.get("sort_order", 0))
    if not slug:
        slug = title.lower().replace(" ", "-")
        slug = "".join(c for c in slug if c.isalnum() or c == "-")
    async with SessionLocal() as s:
        await s.execute(text(
            "INSERT INTO doc_articles (section, title, slug, content, sort_order) VALUES (:s,:t,:sl,:c,:o)"
        ), {"s": section, "t": title, "sl": slug, "c": content, "o": sort_order})
        await s.commit()
    return {"ok": True, "slug": slug}


@app.put("/api/docs/articles/{article_id}")
async def docs_update_article(request: Request, article_id: int, _: bool = Depends(require_auth)):
    body = await request.json()
    sets = []
    params: dict = {"id": article_id}
    for key in ("title", "slug", "section", "content", "sort_order", "is_published"):
        if key in body:
            val = body[key]
            if key == "sort_order":
                val = int(val)
            if key == "is_published":
                val = bool(val)
            sets.append(f"{key} = :{key}")
            params[key] = val
    if not sets:
        raise HTTPException(400, "No fields to update")
    sets.append("updated_at = NOW()")
    sql = f"UPDATE doc_articles SET {', '.join(sets)} WHERE id = :id"
    async with SessionLocal() as s:
        await s.execute(text(sql), params)
        await s.commit()
    return {"ok": True}


@app.delete("/api/docs/articles/{article_id}")
async def docs_delete_article(request: Request, article_id: int, _: bool = Depends(require_auth)):
    async with SessionLocal() as s:
        await s.execute(text("DELETE FROM doc_articles WHERE id = :id"), {"id": article_id})
        await s.commit()
    return {"ok": True}


# FastAPI @app.get() doesn't automatically handle HEAD requests.
# Patch all registered GET routes to also accept HEAD so Googlebot
# and HTTP monitoring tools get proper 200 responses instead of 405.
for _route in app.routes:
    if hasattr(_route, "methods") and isinstance(_route.methods, set) and "GET" in _route.methods:
        _route.methods.add("HEAD")

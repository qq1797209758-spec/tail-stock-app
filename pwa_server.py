"""独立PWA前端与只读API；现有Streamlit入口 app.py 保持不变。"""

from __future__ import annotations

import asyncio
import csv
from datetime import datetime, timedelta
import hmac
import io
import json
import math
import os
from pathlib import Path
import secrets
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
import pandas as pd

from config import SCAN_HISTORY_DATABASE, STRATEGY_VERSION
from services.review_store import SQLiteReviewRepository
from services.scan_history import SQLiteScanHistoryRepository
from services.access_control import (
    SQLiteAccessRepository,
    normalize_invite,
    parse_time,
    secret_hash,
    utc_now,
    verify_password_hash,
)


BASE_DIR = Path(__file__).resolve().parent
PWA_DIR = BASE_DIR / "pwa"
DATABASE_PATH = Path(
    os.getenv("TAIL_STOCK_DATABASE", str(BASE_DIR / SCAN_HISTORY_DATABASE))
).expanduser().resolve()
SHANGHAI = ZoneInfo("Asia/Shanghai")
INVITE_HMAC_SECRET = os.getenv("INVITE_HMAC_SECRET", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")
SESSION_DAYS = max(1, min(int(os.getenv("ACCESS_SESSION_DAYS", "30")), 365))
COOKIE_SECURE = os.getenv("AUTH_COOKIE_SECURE", "true").lower() not in {"0", "false", "no"}
REQUIRE_PERSISTENT_DATABASE = os.getenv("REQUIRE_PERSISTENT_DATABASE", "false").lower() in {
    "1", "true", "yes"
}
PERSISTENCE_READY = (
    not REQUIRE_PERSISTENT_DATABASE
    or (
        bool(os.getenv("TAIL_STOCK_DATABASE", "").strip())
        and BASE_DIR not in DATABASE_PATH.parents
    )
)
AUTH_READY = bool(INVITE_HMAC_SECRET and SESSION_SECRET and PERSISTENCE_READY)
ADMIN_READY = bool(AUTH_READY and ADMIN_USERNAME and ADMIN_PASSWORD_HASH)

app = FastAPI(
    title="尾盘选股助手 API",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

allowed_origins = [
    item.strip()
    for item in os.getenv("PWA_ALLOWED_ORIGINS", "").split(",")
    if item.strip()
]
if allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Accept", "Content-Type", "X-CSRF-Token"],
        allow_credentials=True,
    )


def _finite(value: Any) -> Any:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item"):
        try:
            return _finite(value.item())
        except Exception:
            return str(value)
    return value


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    return [
        {str(key): _finite(value) for key, value in row.items()}
        for row in frame.to_dict(orient="records")
    ]


def _feature(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(str(raw))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _review_repository() -> SQLiteReviewRepository:
    return SQLiteReviewRepository(DATABASE_PATH)


def _access_repository() -> SQLiteAccessRepository:
    if not AUTH_READY:
        raise HTTPException(status_code=503, detail="安全配置缺失")
    return SQLiteAccessRepository(DATABASE_PATH, INVITE_HMAC_SECRET, SESSION_SECRET)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return (forwarded.split(",", 1)[0].strip() or (request.client.host if request.client else "unknown"))


def _ip_hash(request: Request) -> str:
    return secret_hash(SESSION_SECRET, _client_ip(request))


def _allowed_request_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return False
    configured = set(allowed_origins)
    current = f"{request.url.scheme}://{request.url.netloc}"
    return origin == current or origin in configured


def _require_csrf(request: Request) -> None:
    cookie = request.cookies.get("tail_csrf", "")
    header = request.headers.get("x-csrf-token", "")
    if not cookie or not header or not hmac.compare_digest(cookie, header) or not _allowed_request_origin(request):
        raise HTTPException(status_code=403, detail="请求验证失败")


def require_user(tail_session: str = Cookie(default="")) -> dict[str, Any]:
    session = _access_repository().validate_session(tail_session)
    if session is None:
        raise HTTPException(status_code=401, detail="需要邀请码验证")
    return session


def require_admin(tail_admin_session: str = Cookie(default="")) -> dict[str, Any]:
    if not ADMIN_READY:
        raise HTTPException(status_code=503, detail="管理员安全配置缺失")
    session = _access_repository().validate_admin_session(tail_admin_session)
    if session is None:
        raise HTTPException(status_code=401, detail="需要管理员认证")
    return session


@app.middleware("http")
async def security_and_cache_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "server_time": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "strategy_version": STRATEGY_VERSION,
        "auth_ready": AUTH_READY,
        "admin_ready": ADMIN_READY,
        "persistence_ready": PERSISTENCE_READY,
    }


@app.get("/api/auth/csrf")
def csrf_token():
    token = secrets.token_urlsafe(24)
    response = JSONResponse({"csrf_token": token})
    response.set_cookie(
        "tail_csrf", token, max_age=3600, secure=COOKIE_SECURE,
        httponly=False, samesite="lax", path="/",
    )
    return response


@app.get("/api/auth/status")
def auth_status(tail_session: str = Cookie(default="")):
    session = _access_repository().validate_session(tail_session) if AUTH_READY else None
    return {
        "authenticated": session is not None,
        "auth_ready": AUTH_READY,
        "admin_ready": ADMIN_READY,
    }


@app.post("/api/auth/invite")
async def invite_login(request: Request):
    _require_csrf(request)
    repository = _access_repository()
    ip_hash = _ip_hash(request)
    failures = repository.failure_count(ip_hash)
    if failures >= 5:
        await asyncio.sleep(2)
        raise HTTPException(status_code=429, detail="邀请码无效或已过期")
    payload = await request.json()
    code = normalize_invite(str(payload.get("code", "")))
    await asyncio.sleep(min(0.25 * (failures + 1), 2))
    token = repository.activate_invite(
        code,
        device_label=str(payload.get("device_label") or request.headers.get("user-agent", "")),
        session_days=SESSION_DAYS,
    )
    repository.record_attempt(
        ip_hash, request.headers.get("user-agent", ""), successful=token is not None
    )
    if token is None:
        raise HTTPException(status_code=401, detail="邀请码无效或已过期")
    response = JSONResponse({"authenticated": True})
    response.set_cookie(
        "tail_session", token, max_age=SESSION_DAYS * 86400, secure=COOKIE_SECURE,
        httponly=True, samesite="lax", path="/",
    )
    return response


@app.post("/api/auth/logout")
def logout(request: Request, tail_session: str = Cookie(default="")):
    _require_csrf(request)
    if AUTH_READY and tail_session:
        _access_repository().revoke_session(tail_session)
    response = JSONResponse({"authenticated": False})
    response.delete_cookie("tail_session", path="/", secure=COOKIE_SECURE, httponly=True, samesite="lax")
    return response


@app.post("/api/admin/login")
async def admin_login(request: Request):
    _require_csrf(request)
    if not ADMIN_READY:
        raise HTTPException(status_code=503, detail="管理员安全配置缺失")
    payload = await request.json()
    username = str(payload.get("username", ""))
    password = str(payload.get("password", ""))
    username_ok = hmac.compare_digest(username, ADMIN_USERNAME)
    password_ok = verify_password_hash(password, ADMIN_PASSWORD_HASH)
    if not (username_ok and password_ok):
        await asyncio.sleep(1)
        raise HTTPException(status_code=401, detail="管理员认证失败")
    token = _access_repository().create_admin_session(ADMIN_USERNAME)
    response = JSONResponse({"authenticated": True})
    response.set_cookie(
        "tail_admin_session", token, max_age=12 * 3600, secure=COOKIE_SECURE,
        httponly=True, samesite="strict", path="/",
    )
    return response


@app.post("/api/admin/logout")
def admin_logout(request: Request, tail_admin_session: str = Cookie(default="")):
    _require_csrf(request)
    if AUTH_READY and tail_admin_session:
        _access_repository().revoke_admin_session(tail_admin_session)
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(
        "tail_admin_session", path="/", secure=COOKIE_SECURE, httponly=True, samesite="strict"
    )
    return response


@app.get("/api/admin/invites")
def admin_invites(_: dict[str, Any] = Depends(require_admin)):
    return {"items": _access_repository().list_invites()}


@app.post("/api/admin/invites")
async def admin_create_invites(request: Request, admin: dict[str, Any] = Depends(require_admin)):
    _require_csrf(request)
    payload = await request.json()
    expires_at = parse_time(payload.get("expires_at"))
    codes = _access_repository().create_invites(
        int(payload.get("count", 1)),
        max_uses=int(payload.get("max_uses", 1)),
        expires_at=expires_at,
        note=str(payload.get("note", "")),
        created_by=str(admin["username"]),
    )
    return {"codes": codes, "warning": "完整邀请码仅本次显示"}


@app.post("/api/admin/invites/{invite_id}/status")
async def admin_invite_status(
    invite_id: int, request: Request, _: dict[str, Any] = Depends(require_admin)
):
    _require_csrf(request)
    payload = await request.json()
    changed = _access_repository().set_invite_active(
        invite_id, bool(payload.get("is_active")), revoke_sessions=True
    )
    if not changed:
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"updated": True}


@app.post("/api/admin/invites/{invite_id}/revoke-sessions")
def admin_revoke_sessions(
    invite_id: int, request: Request, _: dict[str, Any] = Depends(require_admin)
):
    _require_csrf(request)
    return {"revoked": _access_repository().revoke_invite_sessions(invite_id)}


@app.get("/api/admin/invites.csv")
def admin_invites_csv(_: dict[str, Any] = Depends(require_admin)):
    output = io.StringIO()
    fields = [
        "id", "code_prefix", "note", "max_uses", "used_count", "expires_at",
        "is_active", "created_at", "last_used_at", "created_by",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(_access_repository().list_invites())
    return Response(
        output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; filename=invite-records.csv",
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/top5")
def top5(_: dict[str, Any] = Depends(require_user)):
    frame = _review_repository().review_frame()
    if frame.empty:
        return {"updated_at": None, "recommendation_date": None, "items": []}
    latest = str(frame["recommendation_date"].max())
    latest_frame = frame.loc[frame["recommendation_date"].astype(str).eq(latest)].sort_values(
        "rank", kind="mergesort"
    )
    items = []
    for _, row in latest_frame.head(5).iterrows():
        feature = _feature(row.get("feature_snapshot"))
        items.append(
            {
                "rank": _finite(row.get("rank")),
                "symbol": str(row.get("symbol", "")).zfill(6),
                "name": _finite(row.get("name")),
                "price": _finite(row.get("recommended_price")),
                "change_percent": _finite(feature.get("涨跌幅")),
                "score": _finite(row.get("total_score")),
                "selection_type": _finite(row.get("selection_type")),
                "risk": _finite(row.get("risk_warning")),
                "reason": _finite(row.get("selection_reason")),
                "sector": _finite(row.get("sector")),
                "data_completeness": _finite(row.get("data_completeness")),
                "details": {
                    "volume_ratio": _finite(feature.get("量比")),
                    "turnover_rate": _finite(feature.get("换手率")),
                    "market_cap": _finite(feature.get("总市值")),
                    "vwap_status": _finite(feature.get("VWAP状态")),
                    "late_drawdown": _finite(feature.get("尾盘最大回撤")),
                },
            }
        )
    generated = latest_frame.iloc[0].get("generated_at")
    return {"updated_at": _finite(generated), "recommendation_date": latest, "items": items}


@app.get("/api/reviews/daily")
def daily_review(_: dict[str, Any] = Depends(require_user)):
    frame = _review_repository().review_frame()
    if frame.empty:
        return {"recommendation_date": None, "items": []}
    latest = str(frame["recommendation_date"].max())
    selected = frame.loc[frame["recommendation_date"].astype(str).eq(latest)].sort_values("rank")
    fields = [
        "rank", "symbol", "name", "recommended_price", "open_price", "high_price",
        "low_price", "close_price", "open_return", "high_return", "low_return",
        "close_return", "simulated_return", "selection_type", "total_score",
        "review_status", "conclusion",
    ]
    return {"recommendation_date": latest, "items": _records(selected[fields])}


@app.get("/api/reviews/history")
def review_history(limit: int = 100, _: dict[str, Any] = Depends(require_user)):
    safe_limit = max(1, min(limit, 500))
    frame = _review_repository().review_frame().head(safe_limit)
    fields = [
        "recommendation_date", "strategy_version", "market_state", "rank",
        "symbol", "name", "sector", "selection_type", "total_score",
        "close_return", "simulated_return", "review_status",
    ]
    return {"items": _records(frame[fields]) if not frame.empty else []}


@app.get("/api/status")
def system_status(_: dict[str, Any] = Depends(require_user)):
    history = SQLiteScanHistoryRepository(DATABASE_PATH).list_scans()
    latest = history[0] if history else None
    return {
        "online": True,
        "server_time": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "latest_scan": None
        if latest is None
        else {
            "status": latest.get("status"),
            "started_at": latest.get("started_at"),
            "completed_at": latest.get("completed_at"),
            "data_updated_at": latest.get("data_updated_at"),
            "counts": latest.get("counts"),
        },
        "pending_reviews": _review_repository().pending_count(),
    }


@app.get("/")
def pwa_index():
    return FileResponse(PWA_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(
        PWA_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/service-worker.js")
def service_worker():
    return FileResponse(
        PWA_DIR / "service-worker.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/offline.html")
def offline():
    return FileResponse(PWA_DIR / "offline.html", headers={"Cache-Control": "no-cache"})


app.mount("/assets", StaticFiles(directory=PWA_DIR / "assets"), name="pwa-assets")

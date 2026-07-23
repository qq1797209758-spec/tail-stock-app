"""独立PWA前端与只读API；现有Streamlit入口 app.py 保持不变。"""

from __future__ import annotations

from datetime import datetime
import json
import math
import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import pandas as pd

from config import SCAN_HISTORY_DATABASE, STRATEGY_VERSION
from services.review_store import SQLiteReviewRepository
from services.scan_history import SQLiteScanHistoryRepository


BASE_DIR = Path(__file__).resolve().parent
PWA_DIR = BASE_DIR / "pwa"
DATABASE_PATH = BASE_DIR / SCAN_HISTORY_DATABASE
SHANGHAI = ZoneInfo("Asia/Shanghai")

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
        allow_methods=["GET"],
        allow_headers=["Accept", "Content-Type"],
        allow_credentials=False,
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
    }


@app.get("/api/top5")
def top5():
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
def daily_review():
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
def review_history(limit: int = 100):
    safe_limit = max(1, min(limit, 500))
    frame = _review_repository().review_frame().head(safe_limit)
    fields = [
        "recommendation_date", "strategy_version", "market_state", "rank",
        "symbol", "name", "sector", "selection_type", "total_score",
        "close_return", "simulated_return", "review_status",
    ]
    return {"items": _records(frame[fields]) if not frame.empty else []}


@app.get("/api/status")
def system_status():
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

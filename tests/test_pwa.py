from __future__ import annotations

import json
from pathlib import Path

import pwa_server


ROOT = Path(__file__).resolve().parents[1]
PWA = ROOT / "pwa"


def test_manifest_is_installable_and_icons_exist():
    manifest = json.loads((PWA / "manifest.webmanifest").read_text(encoding="utf-8"))

    assert manifest["name"] == "尾盘选股助手"
    assert manifest["display"] == "standalone"
    assert manifest["start_url"].startswith("/")
    assert manifest["scope"] == "/"
    assert manifest["lang"] == "zh-CN"
    assert {icon["sizes"] for icon in manifest["icons"]} >= {"192x192", "512x512"}
    for icon in manifest["icons"]:
        assert (PWA / icon["src"].lstrip("/")).is_file()


def test_service_worker_never_caches_api_or_non_get_requests():
    worker = (PWA / "service-worker.js").read_text(encoding="utf-8")

    assert "request.method !== \"GET\"" in worker
    assert "url.pathname.startsWith(\"/api/\")" in worker
    assert "cache: \"no-store\"" in worker
    assert "STATIC_CACHE" in worker
    assert "SHELL_CACHE" in worker


def test_api_payloads_and_static_files_are_available():
    health = pwa_server.health()
    top5 = pwa_server.top5()

    assert health["ok"] is True
    assert "server_time" in health
    assert set(top5) == {"updated_at", "recommendation_date", "items"}
    assert len(top5["items"]) <= 5
    assert (PWA / "index.html").is_file()
    assert (PWA / "offline.html").is_file()


def test_non_finite_values_are_not_serialized():
    assert pwa_server._finite(float("inf")) is None
    assert pwa_server._finite(float("-inf")) is None
    assert pwa_server._finite(float("nan")) is None

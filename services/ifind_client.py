"""同花顺 iFinD HTTP 鉴权的最小连接客户端。"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from typing import Any

import requests
import streamlit as st


IFIND_ACCESS_TOKEN_URL = "https://quantapi.51ifind.com/api/v1/get_access_token"
IFIND_REALTIME_QUOTATION_URL = "https://quantapi.51ifind.com/api/v1/real_time_quotation"
IFIND_REQUEST_TIMEOUT_SECONDS = 15
IFIND_ACCESS_TOKEN_CACHE_SECONDS = 6 * 24 * 60 * 60
IFIND_TEST_CODES = ("600000.SH", "000001.SZ")
IFIND_TEST_INDICATORS = ("latest", "open", "high", "low")


class IFindConnectionError(RuntimeError):
    """可安全展示给页面的脱敏连接错误。"""


_cache_lock = threading.Lock()
_cached_access_token: str | None = None
_cached_refresh_token_hash: str | None = None
_cache_expires_at = 0.0


def _read_refresh_token() -> str:
    """按 Streamlit secrets、环境变量的顺序读取 refresh_token。"""
    secret_value = ""
    try:
        secret_value = str(st.secrets.get("IFIND_REFRESH_TOKEN", "")).strip()
    except Exception:
        # 本地未创建 secrets.toml 时，Streamlit 会抛出专用异常。
        secret_value = ""

    refresh_token = secret_value or os.getenv("IFIND_REFRESH_TOKEN", "").strip()
    if not refresh_token:
        raise IFindConnectionError(
            "未配置 IFIND_REFRESH_TOKEN，请在 Streamlit secrets 或环境变量中设置。"
        )
    return refresh_token


def _sanitize_provider_message(value: object) -> str:
    """清理第三方错误文本中的 token 形态和超长内容。"""
    message = str(value or "").strip()
    if not message:
        return "同花顺返回未说明的错误"
    message = re.sub(
        r"(?i)(?:refresh_token|access_token|token)\s*[:=]\s*[^\s,;]+",
        "token=[已隐藏]",
        message,
    )
    message = re.sub(r"\b[A-Za-z0-9_\-]{24,}\b", "[已隐藏]", message)
    return message[:200]


def _extract_provider_error(payload: dict[str, Any]) -> tuple[object, str] | None:
    code = payload.get("errorcode", payload.get("errorCode", payload.get("code")))
    if code in (None, 0, "0"):
        return None
    message = payload.get(
        "errmsg", payload.get("errorMessage", payload.get("message", payload.get("msg")))
    )
    return code, _sanitize_provider_message(message)


def _request_access_token(refresh_token: str) -> str:
    try:
        response = requests.post(
            IFIND_ACCESS_TOKEN_URL,
            headers={
                "Content-Type": "application/json",
                "refresh_token": refresh_token,
            },
            timeout=IFIND_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.Timeout as error:
        raise IFindConnectionError("连接超时，请稍后重试。") from error
    except requests.ConnectionError as error:
        raise IFindConnectionError("网络连接失败，请检查网络或代理设置。") from error
    except requests.HTTPError as error:
        status_code = error.response.status_code if error.response is not None else "未知"
        raise IFindConnectionError(f"HTTP 请求失败（状态码 {status_code}）。") from error
    except requests.RequestException as error:
        raise IFindConnectionError("HTTP 请求失败，请稍后重试。") from error

    try:
        payload = response.json()
    except (requests.JSONDecodeError, ValueError) as error:
        raise IFindConnectionError("同花顺返回了无法解析的 JSON 数据。") from error

    if not isinstance(payload, dict):
        raise IFindConnectionError("同花顺返回的数据格式不正确。")
    provider_error = _extract_provider_error(payload)
    if provider_error is not None:
        code, message = provider_error
        safe_code = _sanitize_provider_message(code)
        raise IFindConnectionError(f"同花顺错误码 {safe_code}：{message}")

    data = payload.get("data")
    access_token = data.get("access_token") if isinstance(data, dict) else None
    if not isinstance(access_token, str) or not access_token.strip():
        raise IFindConnectionError("同花顺响应中缺少有效的 access_token。")
    return access_token.strip()


def get_access_token() -> str:
    """获取并在进程内缓存 access_token；不得将返回值写入页面或日志。"""
    global _cached_access_token, _cached_refresh_token_hash, _cache_expires_at

    refresh_token = _read_refresh_token()
    refresh_token_hash = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
    now = time.monotonic()

    with _cache_lock:
        if (
            _cached_access_token
            and _cached_refresh_token_hash == refresh_token_hash
            and now < _cache_expires_at
        ):
            return _cached_access_token

        access_token = _request_access_token(refresh_token)
        _cached_access_token = access_token
        _cached_refresh_token_hash = refresh_token_hash
        _cache_expires_at = time.monotonic() + IFIND_ACCESS_TOKEN_CACHE_SECONDS
        return access_token


def test_ifind_connection() -> None:
    """仅验证能够取得 access_token，不调用任何行情接口。"""
    get_access_token()


def _request_realtime_payload(access_token: str) -> dict[str, Any]:
    try:
        response = requests.post(
            IFIND_REALTIME_QUOTATION_URL,
            headers={
                "Content-Type": "application/json",
                "access_token": access_token,
                "ifindlang": "cn",
            },
            json={
                "codes": ",".join(IFIND_TEST_CODES),
                "indicators": ",".join(IFIND_TEST_INDICATORS),
            },
            timeout=IFIND_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.Timeout as error:
        raise IFindConnectionError("实时行情请求超时，请稍后重试。") from error
    except requests.ConnectionError as error:
        raise IFindConnectionError("实时行情网络连接失败，请检查网络或代理设置。") from error
    except requests.HTTPError as error:
        status_code = error.response.status_code if error.response is not None else "未知"
        raise IFindConnectionError(
            f"实时行情 HTTP 请求失败（状态码 {status_code}）。"
        ) from error
    except requests.RequestException as error:
        raise IFindConnectionError("实时行情 HTTP 请求失败，请稍后重试。") from error

    try:
        payload = response.json()
    except (requests.JSONDecodeError, ValueError) as error:
        raise IFindConnectionError("同花顺实时行情返回了无法解析的 JSON 数据。") from error
    if not isinstance(payload, dict):
        raise IFindConnectionError("同花顺实时行情返回的数据格式不正确。")

    provider_error = _extract_provider_error(payload)
    if provider_error is not None:
        code, message = provider_error
        raise IFindConnectionError(
            f"同花顺错误码 {_sanitize_provider_message(code)}：{message}"
        )
    return payload


def _value_at(value: object, index: int) -> object:
    if isinstance(value, list):
        return value[index] if index < len(value) else None
    return value if index == 0 else None


def _numeric_value(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_realtime_tables(payload: dict[str, Any]) -> list[dict[str, object]]:
    tables = payload.get("tables")
    if not isinstance(tables, list) or not tables:
        raise IFindConnectionError("同花顺实时行情响应中缺少行情表格。")

    parsed: dict[str, dict[str, object]] = {}
    for item in tables:
        if not isinstance(item, dict) or not isinstance(item.get("table"), dict):
            continue
        raw_codes = item.get("thscode")
        if isinstance(raw_codes, list):
            codes = [str(code).strip() for code in raw_codes]
        elif isinstance(raw_codes, str):
            codes = [code.strip() for code in raw_codes.split(",") if code.strip()]
        else:
            codes = []

        table = item["table"]
        row_count = max(
            [len(codes), 1]
            + [len(value) for value in table.values() if isinstance(value, list)]
        )
        for index in range(row_count):
            code = codes[index] if index < len(codes) else ""
            if code not in IFIND_TEST_CODES:
                continue
            parsed[code] = {
                "股票代码": code,
                "最新价": _numeric_value(_value_at(table.get("latest"), index)),
                "开盘价": _numeric_value(_value_at(table.get("open"), index)),
                "最高价": _numeric_value(_value_at(table.get("high"), index)),
                "最低价": _numeric_value(_value_at(table.get("low"), index)),
                "数据源": "同花顺iFinD",
            }

    missing_codes = [code for code in IFIND_TEST_CODES if code not in parsed]
    if missing_codes:
        raise IFindConnectionError("同花顺实时行情响应缺少指定股票数据。")
    return [parsed[code] for code in IFIND_TEST_CODES]


def fetch_test_realtime_quotes() -> list[dict[str, object]]:
    """仅获取固定两只股票的四项基础实时行情。"""
    access_token = get_access_token()
    payload = _request_realtime_payload(access_token)
    return _parse_realtime_tables(payload)


def post_authenticated_json(
    url: str,
    payload: dict[str, Any],
    *,
    operation: str,
) -> dict[str, Any]:
    """执行通用 iFinD HTTP 请求，仅返回已校验 JSON，不记录认证信息。"""
    access_token = get_access_token()
    try:
        response = requests.post(
            url,
            headers={
                "Content-Type": "application/json",
                "access_token": access_token,
                "ifindlang": "cn",
            },
            json=payload,
            timeout=IFIND_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.Timeout as error:
        raise IFindConnectionError(f"{operation}请求超时，请稍后重试。") from error
    except requests.ConnectionError as error:
        raise IFindConnectionError(f"{operation}网络连接失败。") from error
    except requests.HTTPError as error:
        status_code = error.response.status_code if error.response is not None else "未知"
        raise IFindConnectionError(
            f"{operation} HTTP 请求失败（状态码 {status_code}）。"
        ) from error
    except requests.RequestException as error:
        raise IFindConnectionError(f"{operation} HTTP 请求失败。") from error

    try:
        result = response.json()
    except (requests.JSONDecodeError, ValueError) as error:
        raise IFindConnectionError(f"{operation}返回了无法解析的 JSON 数据。") from error
    if not isinstance(result, dict):
        raise IFindConnectionError(f"{operation}返回的数据格式不正确。")
    provider_error = _extract_provider_error(result)
    if provider_error is not None:
        code, message = provider_error
        raise IFindConnectionError(
            f"同花顺错误码 {_sanitize_provider_message(code)}：{message}"
        )
    return result

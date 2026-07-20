"""AKShare 请求的代理故障降级。"""

from collections.abc import Callable
from contextlib import contextmanager
import os
from threading import Lock
from typing import TypeVar

from requests.exceptions import ProxyError


T = TypeVar("T")
_ENV_LOCK = Lock()
_PROXY_VARIABLES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
_NO_PROXY_VARIABLES = ("NO_PROXY", "no_proxy")


def _is_proxy_error(error: BaseException) -> bool:
    """遍历异常链，判断根因是否为 requests ProxyError。"""
    visited: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in visited:
        if isinstance(current, ProxyError):
            return True
        visited.add(id(current))
        current = current.__cause__ or current.__context__
    return False


@contextmanager
def _temporary_direct_connection():
    """在受锁保护的短时间内忽略进程代理，然后完整恢复环境。"""
    with _ENV_LOCK:
        saved = {
            name: os.environ[name]
            for name in (*_PROXY_VARIABLES, *_NO_PROXY_VARIABLES)
            if name in os.environ
        }
        try:
            for name in _PROXY_VARIABLES:
                os.environ.pop(name, None)
            os.environ["NO_PROXY"] = "*"
            os.environ["no_proxy"] = "*"
            yield
        finally:
            for name in (*_PROXY_VARIABLES, *_NO_PROXY_VARIABLES):
                os.environ.pop(name, None)
            os.environ.update(saved)


def call_with_proxy_fallback(operation: Callable[[], T]) -> T:
    """先按当前环境请求；仅遇到代理错误时，临时直连重试一次。"""
    try:
        return operation()
    except Exception as error:
        if not _is_proxy_error(error):
            raise
        with _temporary_direct_connection():
            return operation()

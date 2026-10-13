"""Request-scoped M1 connection reuse without credential or cookie persistence."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from email.message import Message
import json
import sys
from typing import Any, Iterator
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlsplit

import httpx


class _NoCookies(DefaultCookiePolicy):
    def set_ok(self, cookie: Cookie, request: Any) -> bool:
        return False

    def return_ok(self, cookie: Cookie, request: Any) -> bool:
        return False


class RequestReader:
    """One bounded pool per dashboard request; auth is supplied on every GET."""

    def __init__(self) -> None:
        self.client = httpx.Client(
            timeout=httpx.Timeout(20.0),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
            follow_redirects=False,
            cookies=CookieJar(policy=_NoCookies()),
            headers={"User-Agent": f"Python-urllib/{sys.version_info.major}.{sys.version_info.minor}"},
        )

    def __enter__(self) -> RequestReader:
        return self

    def __exit__(self, *args: Any) -> None:
        self.client.close()

    def get_json(self, url: str, token: str) -> Any:
        original = urlsplit(url)
        for _ in range(10):
            try:
                response = self.client.get(url, headers={"Authorization": f"Bearer {token}"})
            except httpx.TimeoutException:
                raise TimeoutError("数据源请求超时") from None
            except httpx.RequestError:
                raise urllib.error.URLError("数据源请求失败") from None
            if response.status_code in {301, 302, 303, 307, 308} and "location" in response.headers:
                target = urljoin(url, response.headers["location"])
                parsed = urlsplit(target)
                current = urlsplit(url)
                current_port = current.port or (443 if current.scheme == "https" else 80)
                target_port = parsed.port or (443 if parsed.scheme == "https" else 80)
                same_origin = parsed.scheme == current.scheme and target_port == current_port
                upgrade = current.scheme == "http" and parsed.scheme == "https" and current_port == 80 and target_port == 443
                if (parsed.hostname != original.hostname or parsed.username or parsed.password
                        or not (same_origin or upgrade)):
                    raise ValueError("数据源重定向目标不受支持")
                url = target
                continue
            if not 200 <= response.status_code < 300:
                headers = Message()
                for name, value in response.headers.items():
                    headers[name] = value
                raise urllib.error.HTTPError(url, response.status_code, response.reason_phrase, headers, None)
            return json.loads(response.content.decode("utf-8"))
        raise ValueError("数据源重定向次数过多")


_active_reader: ContextVar[RequestReader | None] = ContextVar("m1_request_reader", default=None)


@contextmanager
def reader_scope(enabled: bool = False) -> Iterator[RequestReader | None]:
    with RequestReader() if enabled else nullcontext(None) as reader:
        token = _active_reader.set(reader)
        try:
            yield reader
        finally:
            _active_reader.reset(token)


def get_json(url: str, token: str) -> Any:
    reader = _active_reader.get()
    if reader is not None:
        return reader.get_json(url, token)
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))

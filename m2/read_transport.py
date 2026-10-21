"""Bounded GET-only connection pool for the resident M2 read API."""
import json
from http.cookiejar import CookieJar, DefaultCookiePolicy
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit

import httpx


class _NoCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


class ReadTransport:
    def __init__(self):
        self.client = httpx.Client(
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
            cookies=CookieJar(policy=_NoCookies()), follow_redirects=False,
        )

    def close(self):
        self.client.close()

    def get_json(self, url, token, timeout):
        original = urlsplit(url)
        for _ in range(10):
            try:
                response = self.client.get(
                    url, headers={"Accept": "application/json", "Authorization": "Bearer " + token},
                    timeout=timeout,
                )
            except httpx.TimeoutException:
                raise TimeoutError("数据源请求超时") from None
            except httpx.RequestError:
                raise URLError("数据源请求失败") from None
            if response.status_code in {301, 302, 303, 307, 308} and "location" in response.headers:
                target = urljoin(url, response.headers["location"])
                parsed, current = urlsplit(target), urlsplit(url)
                current_port = current.port or (443 if current.scheme == "https" else 80)
                target_port = parsed.port or (443 if parsed.scheme == "https" else 80)
                same_origin = parsed.scheme == current.scheme and target_port == current_port
                upgrade = current.scheme == "http" and parsed.scheme == "https" and current_port == 80 and target_port == 443
                if parsed.hostname != original.hostname or parsed.username or parsed.password or not (same_origin or upgrade):
                    raise ValueError("数据源重定向目标不受支持")
                url = target
                continue
            if not 200 <= response.status_code < 300:
                raise HTTPError(url, response.status_code, "数据源读取失败", None, None)
            return json.loads(response.content)
        raise ValueError("数据源重定向次数过多")

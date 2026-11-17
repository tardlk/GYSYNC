"""独立 HTTP 传输适配；不使用 MoviePilot 的宿主网络组件。"""

import requests
import httpx


class RequestUtils:
    """为复用代码提供小型 requests 兼容接口。"""

    def __init__(self, headers=None, ua=None, proxies=None, session=None, timeout=30, verify=True, **_):
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.headers = dict(headers or {})
        if ua:
            self.headers["User-Agent"] = ua
        self.proxies, self.timeout, self.verify = proxies, timeout or 30, verify

    def request(self, method, url, **kwargs):
        options = {"timeout": self.timeout, "verify": self.verify, "headers": self.headers}
        if self.proxies:
            options["proxies"] = self.proxies
        options.update(kwargs)
        try:
            return self.session.request(method, url, **options)
        except requests.RequestException:
            # 不传播含 query/API key 的 URL 与异常正文。
            return None

    def get_res(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post_res(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def close(self):
        self.session.close()


class AsyncRequestUtils:
    """TMDB 兼容异步接口；主整理工作使用同步单 worker。"""

    def __init__(self, ua=None, timeout=30, **_):
        self.ua, self.timeout = ua, timeout

    async def get_res(self, url, **kwargs):
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            return await client.get(url, **{k: v for k, v in kwargs.items() if k != "json"})

    async def post_res(self, url, **kwargs):
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            return await client.post(url, **kwargs)

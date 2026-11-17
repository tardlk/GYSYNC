"""有界请求缓存及 fresh 上下文适配。"""
import contextlib
import contextvars
import copy
import functools
import threading
from cachetools import TTLCache

_bypass = contextvars.ContextVar('bypass_cache', default=False)


def cached(maxsize=1024, ttl=3600, skip_none=False, skip_if=None, empty_ttl=None, empty_if=None, **_):
    def decorate(function):
        cache, lock = TTLCache(maxsize=maxsize, ttl=ttl), threading.RLock()
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            key = (tuple(repr(item) for item in args), repr(sorted(kwargs.items())))
            with lock:
                if not _bypass.get() and key in cache:
                    return copy.deepcopy(cache[key])
            value = function(*args, **kwargs)
            # 暂不缓存空搜索结果和失败响应，防止瞬时故障固化。
            if not (skip_none and value is None or skip_if and skip_if(value) or empty_if and empty_if(value)):
                with lock:
                    cache[key] = copy.deepcopy(value)
            return value
        wrapped.cache_clear = cache.clear
        return wrapped
    return decorate


@contextlib.contextmanager
def fresh(enabled=True):
    token = _bypass.set(enabled)
    try:
        yield
    finally:
        _bypass.reset(token)


@contextlib.asynccontextmanager
async def async_fresh(enabled=True):
    with fresh(enabled):
        yield

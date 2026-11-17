"""独立服务注入 MoviePilot 所需的最小配置，不导入 MP 宿主。"""
from types import SimpleNamespace

_provider = lambda: SimpleNamespace(tmdb_api_key='', tmdb_language='zh-CN')


def configure(provider):
    global _provider
    _provider = provider


def get_runtime_setting(name):
    settings = _provider()
    values = {
        'TMDB_API_KEY': settings.tmdb_api_key, 'TMDB_LOCALE': settings.tmdb_language,
        'PROXY': None, 'TMDB_API_DOMAIN': 'api.themoviedb.org',
        'NORMAL_USER_AGENT': 'GuangyaOrganizer/0.1', 'CONF': SimpleNamespace(tmdb=1024, meta=3600),
        'EMPTY_RESULT_CACHE_TTL': 15, 'TMDB_SCRAP_ORIGINAL_IMAGE': False,
        'TMDB_IMAGE_URL': lambda path: 'https://image.tmdb.org/t/p/original' + path if path and path.startswith('/') else path,
    }
    if name not in values:
        raise KeyError('Unsupported MP runtime setting: ' + name)
    return values[name]

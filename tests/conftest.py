"""测试只使用隔离目录和回放数据，不访问真实账号或网络。"""

from types import SimpleNamespace

import pytest

from guangya.service import Service
from guangya.mp_core.schemas.types import MediaType

MOVIE = {
    "id": 27205,
    "media_type": MediaType.MOVIE,
    "title": "盗梦空间",
    "original_title": "Inception",
    "en_title": "Inception",
    "release_date": "2010-07-16",
    "original_language": "en",
    "overview": "A dream & a plan <test>",
    "vote_average": 8.4,
    "genres": [{"id": 878, "name": "科幻"}],
    "genre_ids": [878],
    "credits": {"cast": [], "crew": []},
    "external_ids": {"imdb_id": "tt1375666"},
    "poster_path": None,
    "backdrop_path": None,
}
TV = {
    "id": 1396,
    "media_type": MediaType.TV,
    "name": "绝命毒师",
    "original_name": "Breaking Bad",
    "en_title": "Breaking Bad",
    "first_air_date": "2008-01-20",
    "original_language": "en",
    "overview": "Test TV",
    "vote_average": 8.9,
    "genres": [{"id": 18, "name": "剧情"}],
    "genre_ids": [18],
    "credits": {"cast": [], "crew": []},
    "external_ids": {},
    "seasons": [{"season_number": 1, "episode_count": 7, "air_date": "2008-01-20"}],
}


@pytest.fixture
def service(tmp_path, monkeypatch):
    service = Service(tmp_path / "config")
    source = tmp_path / "downloads"
    target = tmp_path / "media"
    source.mkdir()
    service.config.update(
        {
            "local_roots": [str(source), str(target)],
            "local_source": str(source),
            "local_target": str(target),
            "tmdb_api_key": "fixture-key",
            "scrape": False,
        },
        internal=True,
    )
    monkeypatch.setattr(
        service.media,
        "api",
        lambda: SimpleNamespace(
            match=lambda **kwargs: dict(TV if kwargs.get("mtype") == MediaType.TV else MOVIE),
            get_info=lambda mtype, *args, **kwargs: dict(TV if mtype == MediaType.TV else MOVIE),
            get_tv_season_detail=lambda *args, **kwargs: {
                "episodes": [{"episode_number": 2, "name": "测试集", "air_date": "2008-01-27"}]
            },
            get_movie_images=lambda *args, **kwargs: {},
            get_tv_images=lambda *args, **kwargs: {},
        ),
    )
    return service


def request(service, **overrides):
    config = service.config.snapshot()
    return {
        "storage": "local",
        "source": config.local_source,
        "target": config.local_target,
        "mode": "move",
        "media_type": "movie",
        "tmdb_id": 27205,
        "scrape": False,
        **overrides,
    }


def process(service, result):
    service.process(service.db.get(result["id"]))
    return service.db.get(result["id"])

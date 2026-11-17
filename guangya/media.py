"""使用提取的 MP 原实现解析、匹配、构造命名变量和生成 NFO/图片清单。"""

import copy
import re
from pathlib import Path, PurePosixPath

from jinja2.sandbox import SandboxedEnvironment

from guangya.config import MEDIA_EXTENSIONS, SUB_EXTENSIONS
from guangya.mp_core.domain.metainfo import MetaInfoPath
from guangya.mp_core.domain.meta.runtime import configure_recognition_runtime
from guangya.mp_core.domain.context import MediaInfo, configure_tmdb_image_url_builder
from guangya.mp_core.modules.themoviedb.tmdbapi import TmdbApi
from guangya.mp_core.modules.themoviedb.scraper import TmdbScraper
from guangya.mp_core.naming import TemplateContextBuilder
from guangya.mp_core.runtime.settings import configure, get_runtime_setting
from guangya.mp_core.schemas.types import MediaType
from guangya.mp_core.schemas.tmdb import TmdbEpisode


class ReviewRequired(ValueError):
    """不确定的识别结果需要人工指定媒体。"""


class MediaEngine:
    """配置由独立服务注入；不加载 MP 的 app、Chain、插件、数据库。"""

    def __init__(self, config, database):
        self.config, self.database = config, database
        configure(config.snapshot)
        configure_recognition_runtime(
            media_extensions_provider=lambda: MEDIA_EXTENSIONS + SUB_EXTENSIONS,
            audio_extensions_provider=lambda: (),
            accelerator=None,
        )
        configure_tmdb_image_url_builder(get_runtime_setting("TMDB_IMAGE_URL"))
        self._api_cache = None
        self._api_identity = None
        self.environment = SandboxedEnvironment()
        self.environment.globals.clear()

    def api(self):
        if not self.config.snapshot().tmdb_api_key:
            raise ReviewRequired("请先设置 TMDB API Key")
        settings = self.config.snapshot()
        identity = (settings.tmdb_api_key, settings.tmdb_language)
        if self._api_identity != identity:
            if self._api_cache:
                self._api_cache.close()
            self._api_cache = TmdbApi()
            self._api_identity = identity
        return self._api_cache

    def resolve(self, item, request):
        meta = MetaInfoPath(Path(item.path))
        mtype = {"movie": MediaType.MOVIE, "tv": MediaType.TV}.get(request.get("media_type"))
        if request.get("tmdb_id"):
            if not mtype:
                raise ReviewRequired("手动指定 TMDB ID 时请选择电影或电视剧类型")
            info = self.api().get_info(mtype, int(request["tmdb_id"]), raise_on_connection_error=True)
        else:
            if not meta.name or re.fullmatch(r"(?:19|20)\d{2}", meta.name.strip()):
                raise ReviewRequired("文件名不足以识别媒体，请指定 TMDB ID")
            cache_key = f"recognition:{self.config.snapshot().tmdb_language}:{meta.name}:{meta.year}:{mtype}:{meta.begin_season}"
            info = self.database.value(cache_key)
            if not info:
                info = self.api().match(
                    name=meta.name, mtype=mtype or meta.type, year=meta.year, season_number=meta.begin_season
                )
                if info:
                    # MP 内部把 media_type 标记为枚举，持久化前转回 TMDB 标准类型。
                    info["media_type"] = (
                        "movie" if info.get("media_type") == MediaType.MOVIE or info.get("title") else "tv"
                    )
                    self.database.save_value(cache_key, info)
        if not info:
            raise ReviewRequired("MP 未找到可靠匹配，请指定 TMDB ID")
        info = dict(info)
        info["media_type"] = "movie" if info.get("media_type") in (MediaType.MOVIE, "movie", "电影") else "tv"
        media = MediaInfo()
        media.set_tmdb_info(copy.deepcopy(info))
        if not media.title or not media.tmdb_id:
            raise ReviewRequired("媒体资料不完整")
        if not request.get("tmdb_id") and re.fullmatch(r"(?:19|20)\d{2}", media.title.strip()):
            raise ReviewRequired("识别标题疑似只有年份，请人工确认 TMDB ID")
        if media.type == MediaType.TV and meta.begin_episode is None:
            raise ReviewRequired("电视剧未能解析集数，请修正文件名后重试")
        return meta, media, info

    def plan(self, item, request, settings):
        meta, media, info = self.resolve(item, request)
        episodes = None
        if media.type == MediaType.TV:
            season = meta.begin_season if meta.begin_season is not None else 1
            detail = self.api().get_tv_season_detail(media.tmdb_id, season)
            episodes = [TmdbEpisode(**ep) for ep in detail.get("episodes", [])]
        context = TemplateContextBuilder().build(
            meta=meta,
            mediainfo=media,
            file_extension=Path(item.path).suffix,
            episodes_info=episodes,
            include_raw_objects=False,
        )
        context["category"] = ""
        template = settings["movie_template"] if media.type == MediaType.MOVIE else settings["tv_template"]
        if len(template) > 4000:
            raise ValueError("命名模板过长")
        rendered = self.environment.from_string(template).render(context).strip()
        relative = PurePosixPath(rendered)
        if not rendered or relative.is_absolute() or ".." in relative.parts or "\\" in rendered or "\x00" in rendered:
            raise ValueError("命名模板生成了无效路径")
        if relative.suffix.lower() != Path(item.path).suffix.lower():
            raise ValueError("命名模板必须保留 {{fileExt}} 扩展名")
        prefix = media.type.value if settings["type_folders"] else ""
        target = str(Path(request["target"]) / prefix / relative)
        return {
            "source": item.path,
            "target": target,
            "identity": item.model_dump(),
            "title": media.title_year,
            "tmdb_id": media.tmdb_id,
            "media_type": "movie" if media.type == MediaType.MOVIE else "tv",
            "tmdb": info,
            "state": "planned",
            "role": "media",
        }

    def scrape(self, plan):
        media = MediaInfo()
        media.set_tmdb_info(copy.deepcopy(plan["tmdb"]))
        meta = MetaInfoPath(Path(plan["source"]))
        scraper = TmdbScraper()
        scraper._meta_tmdb = self.api()
        scraper._img_tmdb = scraper._meta_tmdb
        target = Path(plan["target"])
        documents = {}
        if media.type == MediaType.TV:
            season = meta.begin_season if meta.begin_season is not None else 1
            documents[str(target.with_suffix(".nfo"))] = scraper.get_metadata_nfo(
                meta, media, season=season, episode=meta.begin_episode
            )
            documents[str(target.parent / "season.nfo")] = scraper.get_metadata_nfo(meta, media, season=season)
            documents[str(target.parent.parent / "tvshow.nfo")] = scraper.get_metadata_nfo(meta, media)
            images = {
                str(target.parent / name): url for name, url in scraper.get_metadata_img(media, season=season).items()
            }
            images.update(
                {str(target.parent.parent / name): url for name, url in scraper.get_metadata_img(media).items()}
            )
        else:
            documents[str(target.with_suffix(".nfo"))] = scraper.get_metadata_nfo(meta, media)
            images = {str(target.parent / name): url for name, url in scraper.get_metadata_img(media).items()}
        return {path: text.encode("utf-8") for path, text in documents.items() if text}, images

# Extracted from MoviePilot; GPL-3.0. Runtime imports adapted.
from __future__ import annotations
import re
import socket
from typing import Any, Dict, List, Optional, Union, overload
from guangya.mp_core.domain.context import MediaInfo, MusicInfo, TorrentInfo
from guangya.mp_core.domain.meta.metabase import MetaBase
from guangya.mp_core.domain.meta.metamusic import MetaMusic
from guangya.mp_core.schemas.tmdb import TmdbEpisode
from guangya.mp_core.schemas.types import MUSIC_ENTITY_ALBUM
from guangya.mp_core.foundation import size as size_tools
from guangya.mp_core.foundation.text import convert as zhconv_convert
from types import SimpleNamespace
TransferInfo = Any
def get_chain_runtime_config_snapshot():
    return SimpleNamespace(music_metadata_to_simplified=False)
_ALBUM_TRAILING_YEAR_RE = re.compile(r"(?:[\s\u3000]*[\(\[（【]\s*(?:19|20)\d{2}\s*[\)\]）】])+$")

class TemplateContextBuilder:
    """
    模板上下文构建器。

    无状态实现：所有 ``_add_*`` 方法均为静态方法，接受并就地修改调用方提供的
    ``context`` 字典。``build`` 每次调用都基于一份新的本地字典装填后返回，
    实例自身不持有任何中间状态——可以被多线程共享调用而不会产生互相串味的
    ``rename_dict``，配合 ``settings.TRANSFER_THREADS > 1`` 的并发整理场景安全。

    保留为类（而非自由函数）是为了向后兼容现有调用方式
    （``TemplateHelper().builder.build(...)``）。
    """

    def build(
            self,
            meta: Optional[MetaBase] = None,
            mediainfo: Optional[Union[MediaInfo, MusicInfo]] = None,
            torrentinfo: Optional[TorrentInfo] = None,
            transferinfo: Optional[TransferInfo] = None,
            file_extension: Optional[str] = None,
            episodes_info: Optional[List[TmdbEpisode]] = None,
            include_raw_objects: bool = True,
            aggregate_music_album: bool = False,
            **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        构建一次性渲染上下文字典。

        每次调用都新建本地 ``context`` 字典，依次填充各业务来源和当前实例的
        主机名后返回过滤掉 None 值的副本，调用之间互不影响。
        音乐在字段取值完成后统一按开关转简体，避免原始标签覆盖在线转换结果。

        :param meta: 媒体元数据
        :param mediainfo: 识别的媒体信息
        :param torrentinfo: 种子信息
        :param transferinfo: 整理结果信息
        :param file_extension: 文件扩展名
        :param episodes_info: 当前季的全部集信息
        :param include_raw_objects: 是否在 dict 里附带原始对象引用（``__meta__`` 等）
        :param aggregate_music_album: 是否按整专聚合（通知场景）：专辑实体
            批量下载/入库只发一条通知，标题取专辑名且不展示单曲序号；
            重命名等逐文件场景必须为 False 以保留每个文件的曲名和曲序
        :return: 渲染上下文字典
        """
        context: Dict[str, Any] = {}
        self._add_episode_details(context, meta, episodes_info)
        self._add_media_info(context, mediainfo, aggregate_music_album)
        self._add_transfer_info(context, transferinfo)
        self._add_torrent_info(context, torrentinfo)
        self._add_file_info(context, file_extension)
        if isinstance(meta, MetaMusic) or isinstance(mediainfo, MusicInfo):
            self._simplify_music_context(context)
        context.update(kwargs, instance_name=socket.gethostname())

        if include_raw_objects:
            self._add_raw_objects(context, meta, mediainfo, torrentinfo, transferinfo, episodes_info)

        return {k: v for k, v in context.items() if v is not None}

    @staticmethod
    def _simplify_music_context(context: Dict[str, Any]) -> None:
        """
        只转换最终音乐展示字段，保留原始文件名、标签、媒体身份和取值优先级。
        """
        if not get_chain_runtime_config_snapshot().music_metadata_to_simplified:
            return
        for field_name in ("title", "name", "artist", "album", "album_artist", "title_year", "version"):
            value = context.get(field_name)
            if isinstance(value, str):
                context[field_name] = zhconv_convert(value, "zh-hans")
        if "artists" in context:
            context["artists"] = [zhconv_convert(artist, "zh-hans") for artist in context["artists"]]

    @classmethod
    def _add_media_info(
            cls,
            context: Dict[str, Any],
            mediainfo: Optional[Union[MediaInfo, MusicInfo]],
            aggregate_music_album: bool = False,
    ) -> None:
        """
        将 MediaInfo 中的标题、季年份、海报等业务字段就地写入 ``context``。

        会读取 ``context`` 中由 ``_add_episode_details`` 先填好的 ``season`` /
        ``year`` / ``title_year`` 占位，保证电视剧场景下季/年优先沿用 meta 解析值；
        音乐场景保留曲目级标签；已识别专辑实体时以所选数据源覆盖专辑级字段；
        通知场景（``aggregate_music_album=True``）下整专批量以专辑为标题主体。
        """
        if not mediainfo:
            return
        if isinstance(mediainfo, MusicInfo):
            # 专辑实体批量下载/入库只发一条通知：标题取专辑名、不展示单曲
            # 序号；重命名等逐文件场景保持 False，继续使用文件自己的曲名和曲序。
            is_album_entity = mediainfo.music_type == MUSIC_ENTITY_ALBUM
            is_album_context = aggregate_music_album and is_album_entity
            has_album_identity = bool(is_album_entity and mediainfo.media_source and mediainfo.media_id)
            # 专辑场景以识别结果的专辑名为标题；整专年份以识别结果为准，
            # 逐文件场景沿用 meta 解析年份，保证文件级年份优先。
            year = (
                mediainfo.year
                if (has_album_identity and mediainfo.year)
                else (context.get("year") or mediainfo.year)
            )
            if is_album_context and mediainfo.album:
                title = cls.__strip_album_trailing_year(
                    cls.__convert_invalid_characters(mediainfo.album), year
                )
            else:
                title = context.get("title") or cls.__convert_invalid_characters(mediainfo.title)
            artists = context.get("artists") or [
                cls.__convert_invalid_characters(item) for item in mediainfo.artists
            ]
            artist = context.get("artist") or cls.__convert_invalid_characters(mediainfo.artist)
            # 标签/目录名自带的尾部年份会被重命名模板的 `({{year}})` 再次追加，
            # 统一剥离避免生成 "专辑 (2018) (2018)" 这类重复年份目录（issue #6355）
            album_value = mediainfo.album or mediainfo.title if has_album_identity else context.get("album") or mediainfo.album
            album = cls.__strip_album_trailing_year(
                cls.__convert_invalid_characters(album_value),
                year,
            )
            album_artist_value = mediainfo.album_artist or mediainfo.artist \
                if has_album_identity else context.get("album_artist") or mediainfo.album_artist
            album_artist = cls.__convert_invalid_characters(album_artist_value)
            disc_number = context.get("disc_number") or mediainfo.disc_number
            track_number = (
                None
                if is_album_context
                else (context.get("track_number") or mediainfo.track_number)
            )
            context.update({
                "type": (mediainfo.type.value if mediainfo.type else ""),
                "title": title,
                "name": title if is_album_context else (context.get("name") or title),
                "artists": artists,
                "artist": artist,
                "album": album,
                "album_artist": album_artist,
                "year": year,
                "title_year": f"{title} ({year})" if title and year else title,
                "disc_number": disc_number,
                "track_number": track_number,
                "track": f"{track_number:02d}" if track_number else None,
                "total_tracks": context.get("total_tracks") or mediainfo.total_tracks,
                "duration": context.get("duration") or mediainfo.duration,
                "isrc": context.get("isrc") or mediainfo.isrc,
                "version": context.get("version") or mediainfo.version,
                "audio_format": context.get("audio_format") or mediainfo.audio_format,
                "audio_lossless": context.get("audio_lossless")
                if context.get("audio_lossless") is not None else mediainfo.audio_lossless,
                "audio_quality": context.get("audio_quality") or mediainfo.audio_quality,
                "audio_specs": context.get("audio_specs") or mediainfo.audio_specs,
                "bit_depth": context.get("bit_depth") or mediainfo.bit_depth,
                "sample_rate": context.get("sample_rate") or mediainfo.sample_rate,
                "sample_rate_khz": context.get("sample_rate_khz")
                or (f"{mediainfo.sample_rate / 1000:g}" if mediainfo.sample_rate else None),
                "bitrate": context.get("bitrate") or mediainfo.bitrate,
                "bitrate_kbps": context.get("bitrate_kbps")
                or (round(mediainfo.bitrate / 1000) if mediainfo.bitrate else None),
                "category": mediainfo.category,
                "poster": mediainfo.get_poster_image(),
                "backdrop": mediainfo.get_backdrop_image(),
                "media_source": mediainfo.media_source,
                "media_id": mediainfo.media_id,
            })
            return
        season_fmt = f"S{mediainfo.season:02d}" if mediainfo.season is not None else None
        media_source = mediainfo.media_source
        media_id = mediainfo.media_id
        base_info = {
            # 标题
            "title": cls.__convert_invalid_characters(mediainfo.title),
            # 英文标题
            "en_title": cls.__convert_invalid_characters(mediainfo.en_title),
            # 原语种标题
            "original_title": cls.__convert_invalid_characters(mediainfo.original_title),
            # 季号
            "season": context.get("season") or mediainfo.season,
            # 季号格式为 Sxx
            "season_fmt": context.get("season_fmt") or season_fmt,
            # 年份
            "year": mediainfo.year or context.get("year"),
            # 媒体标题 + 年份
            "title_year": mediainfo.title_year or context.get("title_year"),
        }

        _meta_season = context.get("season")
        media_info = {
            # 类型
            "type": (mediainfo.type.value if mediainfo.type else ""),
            # 类别
            "category": mediainfo.category,
            # 评分
            "vote_average": mediainfo.vote_average,
            # 海报
            "poster": mediainfo.get_poster_image(),
            # 背景图
            "backdrop": mediainfo.get_backdrop_image(),
            # 季年份根据season值获取
            "season_year": mediainfo.season_years.get(
                int(_meta_season),
                None) if (mediainfo.season_years and _meta_season) else None,
            # 演员
            "actors": '、 '.join([actor['name'] for actor in mediainfo.actors[:5]]),
            # 简介
            "overview": mediainfo.overview,
            # TMDB 媒体标识
            "tmdbid": mediainfo.tmdb_id,
            # IMDb 媒体标识
            "imdbid": mediainfo.imdb_id,
            # 豆瓣ID
            "doubanid": mediainfo.douban_id,
            # Bangumi 条目标识
            "bangumiid": mediainfo.bangumi_id,
            # AniList 条目标识
            "anilistid": mediainfo.anilist_id,
            # 当前媒体数据源
            "media_source": media_source,
            # 当前数据源原生ID
            "media_id": str(media_id) if media_id is not None else None,
        }
        context.update({**base_info, **media_info})

    @classmethod
    def _add_episode_details(
            cls,
            context: Dict[str, Any],
            meta: Optional[MetaBase],
            episodes: Optional[List[TmdbEpisode]],
    ) -> None:
        """
        将 meta 解析得到的剧集级信息、技术字段写入 ``context``，并尝试匹配
        TMDB 集详情填入 ``episode_title`` / ``episode_date``。
        """
        if not meta:
            return
        if isinstance(meta, MetaMusic):
            context.update({
                "original_name": meta.org_string or meta.title,
                "name": cls.__convert_invalid_characters(meta.title),
                "title": cls.__convert_invalid_characters(meta.title),
                "artists": [cls.__convert_invalid_characters(item) for item in meta.artists],
                "artist": cls.__convert_invalid_characters(meta.artist),
                # 标签专辑名常自带尾部年份，与模板独立追加的年份去重（issue #6355）
                "album": cls.__strip_album_trailing_year(
                    cls.__convert_invalid_characters(meta.album or ""), meta.year
                ),
                "album_artist": cls.__convert_invalid_characters(meta.album_artist or ""),
                "year": meta.year,
                "disc_number": meta.disc_number,
                "track_number": meta.track_number,
                "track": f"{meta.track_number:02d}" if meta.track_number else None,
                "total_discs": meta.total_discs,
                "total_tracks": meta.total_tracks,
                "audio_format": meta.audio_format,
                "audio_lossless": meta.audio_lossless,
                "audio_quality": meta.audio_quality,
                "audio_specs": meta.audio_specs,
                "bit_depth": meta.bit_depth,
                "sample_rate": meta.sample_rate,
                "sample_rate_khz": f"{meta.sample_rate / 1000:g}" if meta.sample_rate else None,
                "bitrate": meta.bitrate,
                "bitrate_kbps": round(meta.bitrate / 1000) if meta.bitrate else None,
                "duration": meta.duration,
                "isrc": meta.isrc,
                "version": meta.version,
            })
            return

        episode_data: Dict[str, Optional[str]] = {"episode_title": None, "episode_date": None}
        if meta.begin_episode and episodes:
            for episode in episodes:
                if episode.episode_number == meta.begin_episode:
                    episode_data.update({
                        "episode_title": cls.__convert_invalid_characters(episode.name or ""),
                        "episode_date": episode.air_date if episode.air_date else None
                    })
                    break

        meta_info = {
            # 原文件名
            "original_name": meta.title,
            # 识别名称（优先使用中文）
            "name": meta.name,
            # 识别的英文名称（可能为空）
            "en_name": meta.en_name,
            # 年份
            "year": meta.year,
            # 名字 + 年份
            "title_year": context.get("title_year") or "%s (%s)" % (
                meta.name, meta.year) if meta.year else meta.name,
            # 季号
            "season": meta.season_seq,
            # 季号格式为 Sxx
            "season_fmt": meta.season,
            # 集号
            "episode": meta.episode_seqs,
            # 当前季总集数
            "total_episodes": len(episodes) if episodes else 0,
            # 季集 SxxExx
            "season_episode": "%s%s" % (meta.season, meta.episode),
            # 段/节
            "part": meta.part,
            # 自定义占位符
            "customization": meta.customization,
            # 每秒帧数
            "fps": meta.fps,
        }

        tech_metadata = {
            # 资源类型
            "resourceType": meta.resource_type,
            # 特效
            "effect": meta.resource_effect,
            # 版本
            "edition": meta.edition,
            # 分辨率
            "videoFormat": meta.resource_pix,
            # 质量
            "resource_term": meta.resource_term,
            # 制作组/字幕组
            "releaseGroup": meta.resource_team,
            # 视频编码
            "videoCodec": meta.video_encode,
            # 视频位深
            "videoBit": meta.video_bit,
            # 音频编码
            "audioCodec": meta.audio_encode,
            # 流媒体平台
            "webSource": meta.web_source,
        }
        context.update({**meta_info, **tech_metadata, **episode_data})

    @staticmethod
    def _add_torrent_info(context: Dict[str, Any], torrentinfo: Optional[TorrentInfo]) -> None:
        """
        将种子信息写入 ``context``，描述字段会去除 HTML 标签。

        副作用提醒：当 ``torrentinfo.description`` 包含 HTML 时，会就地清洗
        原对象的 description 字段——保留原始行为，避免破坏现有调用方对清洗后
        描述的依赖。
        """
        if not torrentinfo:
            return
        size: Union[str, int] = 0
        if torrentinfo.size:
            if str(torrentinfo.size).replace(".", "").isdigit():
                size = size_tools.format_compact_size(torrentinfo.size)
            else:
                size = str(torrentinfo.size)
        else:
            size = 0

        if torrentinfo.description:
            html_re = re.compile(r'<[^>]+>', re.S)
            description = html_re.sub('', torrentinfo.description)
            torrentinfo.description = re.sub(r'<[^>]+>', '', description)

        torrent_info = {
            # 种子标题
            "torrent_title": torrentinfo.title,
            # 发布时间
            "pubdate": torrentinfo.pubdate,
            # 免费剩余时间
            "freedate": torrentinfo.freedate_diff,
            # 做种数
            "seeders": torrentinfo.seeders,
            # 促销信息
            "volume_factor": torrentinfo.volume_factor,
            # 是否受下载后必须做种的 Hit&Run 规则约束
            "hit_and_run": "是" if torrentinfo.hit_and_run else "否",
            # 种子标签
            "labels": ' '.join(torrentinfo.labels),
            # 描述
            "description": torrentinfo.description,
            # 站点名称
            "site_name": torrentinfo.site_name,
            # 种子大小
            "size": size,
        }
        context.update(torrent_info)

    @staticmethod
    def _add_transfer_info(context: Dict[str, Any], transferinfo: Optional[TransferInfo]) -> None:
        """
        将整理结果（类型、文件数、总大小、错误信息）写入 ``context``。
        """
        if not transferinfo:
            return
        ctx = {
            "transfer_type": transferinfo.transfer_type,
            "file_count": transferinfo.file_count,
            "total_size": size_tools.format_compact_size(transferinfo.total_size or 0),
            "err_msg": transferinfo.message,
        }
        context.update(ctx)

    @staticmethod
    def _add_file_info(context: Dict[str, Any], file_extension: Optional[str]) -> None:
        """
        将文件扩展名写入 ``context.fileExt``。
        """
        if not file_extension:
            return
        file_info = {
            # 文件后缀
            "fileExt": file_extension,
        }
        context.update(file_info)

    @staticmethod
    def _add_raw_objects(
            context: Dict[str, Any],
            meta: Optional[MetaBase],
            mediainfo: Optional[Union[MediaInfo, MusicInfo]],
            torrentinfo: Optional[TorrentInfo],
            transferinfo: Optional[TransferInfo],
            episodes_info: Optional[List[TmdbEpisode]],
    ) -> None:
        """
        以双下划线键名将原始对象引用写入 ``context``。

        约定：消费方仅读不写，避免在事件回调里改这些对象污染下游流程。
        """
        raw_objects = {
            # 文件元数据
            "__meta__": meta,
            # 识别的媒体信息
            "__mediainfo__": mediainfo,
            # 种子信息
            "__torrentinfo__": torrentinfo,
            # 文件转移信息
            "__transferinfo__": transferinfo,
            # 当前季的全部集信息
            "__episodes_info__": episodes_info,
        }
        context.update(raw_objects)

    @staticmethod
    @overload
    def __convert_invalid_characters(filename: str) -> str:
        """
        按文件名输入是否存在声明对应的清理结果，保留 None 的缺省语义。
        """
        ...

    @staticmethod
    @overload
    def __convert_invalid_characters(filename: None) -> None:
        """
        按文件名输入是否存在声明对应的清理结果，保留 None 的缺省语义。
        """
        ...

    @staticmethod
    def __convert_invalid_characters(filename: Optional[str]) -> Optional[str]:
        """
        将不支持的字符转换为全角字符
        """
        if not filename:
            return filename
        invalid_characters = r'\/:*?"<>|'
        halfwidth_chars = "".join([chr(i) for i in range(33, 127)])
        fullwidth_chars = "".join([chr(i + 0xFEE0) for i in range(33, 127)])
        translation_table = str.maketrans(halfwidth_chars, fullwidth_chars)
        for char in invalid_characters:
            filename = filename.replace(char, char.translate(translation_table))
        return filename

    @staticmethod
    def __strip_album_trailing_year(
            album: Optional[str], year: Optional[Union[int, str]]
    ) -> Optional[str]:
        """
        去除专辑名尾部的括号年份标记（如 ``欲望反光 (2018)`` -> ``欲望反光``）。

        音频标签或下载目录常把发行年份写进专辑名，而重命名模板会独立追加
        ``({{year}})``，两者叠加会生成 "专辑 (2018) (2018)" 这类重复年份目录；
        仅当存在可独立渲染的年份时才剥离，避免丢失只存在于专辑名中的年份信息。
        """
        if not album or not year:
            return album
        return _ALBUM_TRAILING_YEAR_RE.sub("", album) or album

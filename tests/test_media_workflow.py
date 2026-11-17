"""真实 MP 解析、命名和 NFO 加上真实本地文件操作。"""

from pathlib import Path
from xml.dom import minidom

import pytest

from guangya.storage import independent_paths
from guangya.mp_core.domain.metainfo import MetaInfoPath
from conftest import request, process


def media_files(service):
    source = Path(service.config.snapshot().local_source)
    video = source / "Inception.2010.1080p.BluRay.mkv"
    video.write_bytes(b"test media bytes")
    subtitle = video.with_suffix(".zh.ass")
    subtitle.write_text("test subtitle")
    (source / "说明.txt").write_text("keep me")
    return video, subtitle


def test_original_mp_parser_handles_movie_and_tv(service):
    movie = MetaInfoPath(Path("/downloads/Inception.2010.1080p.BluRay.mkv"))
    tv = MetaInfoPath(Path("/downloads/Breaking.Bad.S01E02.1080p.mkv"))
    assert movie.name == "Inception"
    assert movie.year == "2010"
    assert tv.name == "Breaking Bad"
    assert (tv.begin_season, tv.begin_episode) == (1, 2)


def test_preview_is_read_only_and_uses_mp_template(service):
    video, subtitle = media_files(service)
    job = process(service, service.submit_organize(request(service)))
    assert job["state"] == "preview_ready"
    assert len(job["result"]["plans"]) == 2
    assert job["result"]["plans"][0]["target"].endswith("/电影/盗梦空间 (2010)/盗梦空间 (2010).mkv")
    assert video.exists() and subtitle.exists()
    assert not Path(service.config.snapshot().local_target).exists()
    assert not service.db.operations(job["id"])


@pytest.mark.parametrize("mode", ["move", "copy", "link"])
def test_confirm_executes_real_files_and_preserves_unrelated_notes(service, mode):
    video, subtitle = media_files(service)
    job = process(service, service.submit_organize(request(service, mode=mode)))
    service.execute_preview(job["id"])
    job = process(service, {"id": job["id"]})
    assert job["state"] == "completed"
    for plan in job["result"]["plans"]:
        target = Path(plan["target"])
        assert target.exists()
        if mode == "move":
            assert not Path(plan["source"]).exists()
        else:
            assert Path(plan["source"]).exists()
        if mode == "link":
            assert target.stat().st_ino == Path(plan["source"]).stat().st_ino
    assert (video.parent / "说明.txt").read_text() == "keep me"
    assert all(op["state"] == "completed" for op in service.db.operations(job["id"]))


def test_mp_nfo_survives_persisted_preview_roundtrip(service):
    video, _ = media_files(service)
    job = process(service, service.submit_organize(request(service, scrape=True)))
    plan = job["result"]["plans"][0]
    assert plan["tmdb"]["media_type"] == "movie"
    documents, _ = service.media.scrape(plan)
    doc = minidom.parseString(next(iter(documents.values())))
    assert doc.documentElement.tagName == "movie"
    assert doc.getElementsByTagName("title")[0].firstChild.nodeValue == "盗梦空间"
    assert doc.getElementsByTagName("plot")[0].firstChild.nodeValue == "A dream & a plan <test>"


def test_tv_naming_uses_mp_season_episode_and_episode_title(service):
    video = Path(service.config.snapshot().local_source) / "Breaking.Bad.S01E02.mkv"
    video.write_bytes(b"tv")
    job = process(service, service.submit_organize(request(service, media_type="tv", tmdb_id=1396)))
    assert job["state"] == "preview_ready"
    assert "/Season 1/" in job["result"]["plans"][0]["target"]
    assert "S01E02" in job["result"]["plans"][0]["target"]


def test_changed_source_after_preview_is_not_moved(service):
    video, _ = media_files(service)
    job = process(service, service.submit_organize(request(service)))
    video.write_bytes(b"a different version")
    service.execute_preview(job["id"])
    job = process(service, {"id": job["id"]})
    assert job["state"] == "needs_review"
    assert video.exists()
    assert not service.db.operations(job["id"])


def test_target_collision_never_overwrites(service):
    video, _ = media_files(service)
    job = process(service, service.submit_organize(request(service)))
    target = Path(job["result"]["plans"][0]["target"])
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing library file")
    service.execute_preview(job["id"])
    job = process(service, {"id": job["id"]})
    assert job["state"] == "needs_review"
    assert target.read_bytes() == b"existing library file"
    assert video.exists()


def test_symlink_and_outside_root_are_rejected(service, tmp_path):
    outside = tmp_path / "private"
    outside.mkdir()
    link = Path(service.config.snapshot().local_source) / "escape"
    link.symlink_to(outside, target_is_directory=True)
    for path in (outside, link):
        with pytest.raises(ValueError):
            service.storage("local").path(str(path))


@pytest.mark.parametrize(
    "source,target", [("/", "/media"), ("/a", "/a"), ("/a", "/a/b"), ("/a/b", "/a"), ("/a/../b", "/media")]
)
def test_invalid_or_overlapping_directories(source, target):
    with pytest.raises(ValueError):
        independent_paths(source, target)


def test_malicious_template_cannot_escape_or_access_objects(service):
    video, _ = media_files(service)
    service.config.update({"movie_template": "../outside{{fileExt}}"})
    job = process(service, service.submit_organize(request(service)))
    assert job["state"] == "needs_review"
    assert video.exists()


def test_numeric_year_name_pauses_without_auto_selection(service):
    video = Path(service.config.snapshot().local_source) / "2026.IMAX.mkv"
    video.write_bytes(b"not a title")
    job = process(service, service.submit_organize(request(service, tmdb_id=None, media_type="auto")))
    assert job["state"] == "needs_review"
    assert video.exists()


def test_recovery_does_not_replay_unknown_writes(service):
    job = service.db.create("upload", {"paths": [], "target": "/x"})
    service.db.set(job["id"], "uploading", "in flight")
    service.db.operation(job["id"], "/source", "/target", "running", {})
    service.db.recover()
    assert service.db.get(job["id"])["state"] == "needs_review"
    assert service.db.next() is None
    with pytest.raises(ValueError, match="结果不明"):
        service.retry(job["id"])


def test_migration_can_create_a_review_task_without_ever_queueing_it(service):
    result = service.db.create("legacy_share", {"source": "/old"}, state="needs_review")
    assert result["state"] == "needs_review"
    assert service.db.get(result["id"])["state"] == "needs_review"
    assert service.db.next() is None


@pytest.mark.parametrize("mode", ["copy", "move"])
def test_source_modified_during_copy_is_preserved_and_requires_review(service, monkeypatch, mode):
    import errno
    import shutil

    video, _ = media_files(service)
    job = process(service, service.submit_organize(request(service, mode=mode)))
    original = shutil.copyfileobj

    def concurrent_change(source, target, *args):
        original(source, target, *args)
        video.write_bytes(b"a newly written version during copy")

    monkeypatch.setattr("guangya.storage.shutil.copyfileobj", concurrent_change)
    if mode == "move":

        def cross_device(*args):
            raise OSError(errno.EXDEV, "simulated different filesystem")

        monkeypatch.setattr("guangya.storage.os.link", cross_device)
    service.execute_preview(job["id"])
    job = process(service, {"id": job["id"]})
    assert job["state"] == "needs_review"
    assert video.exists()
    assert video.read_bytes() == b"a newly written version during copy"

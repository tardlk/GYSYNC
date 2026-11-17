"""转存/上传异常的副作用边界，不调用真实云盘。"""

from pathlib import Path
from unittest.mock import Mock

import pytest

from guangya.models import FileItem
from guangya.share_protocol import parse_share
from conftest import process

LINK = "https://www.guangyapan.com/s/fixture_share_123?code=abcd#/share"


def fake_cloud(service, monkeypatch, timeout=False):
    client, api = Mock(), Mock()
    client.share_access.return_value = {"accessToken": "temporary-share-access"}
    folder = FileItem(path="/pending/job/", type="dir", fileid="folder")
    api.list_strict.return_value = []
    monkeypatch.setattr(service, "cloud", lambda *args, **kwargs: (client, api))
    monkeypatch.setattr("guangya.service.read_manifest", lambda *args: (["r1"], {"x.mkv": {"type": "file", "size": 4}}))
    monkeypatch.setattr("guangya.service.ensure_folder", lambda *args: folder)
    monkeypatch.setattr("guangya.service.saved_manifest", lambda *args: {"x.mkv": {"type": "file", "size": 4}})
    return client, api


def test_dedup_is_by_share_id_not_extraction_code(service, monkeypatch):
    fake_cloud(service, monkeypatch)
    a = service.submit_share(LINK)
    b = service.submit_share(LINK.replace("abcd", "other"))
    assert b["duplicate"] is True
    assert b["id"] == a["id"]
    public = str(service.db.list())
    assert "fixture_share_123" not in public
    assert "abcd" not in public


def test_save_is_persisted_before_write_and_timeout_is_not_replayed(service, monkeypatch):
    client, api = fake_cloud(service, monkeypatch)
    result = service.submit_share(LINK)

    def restore(*args):
        assert service.db.get(result["id"])["state"] == "saving"
        raise TimeoutError()

    client.restore_share.side_effect = restore
    job = process(service, result)
    assert job["state"] == "needs_review"
    client.restore_share.assert_called_once()
    service.db.recover()
    assert service.db.next() is None
    with pytest.raises(ValueError):
        service.retry(job["id"])


def test_verified_save_continues_to_organize_once(service, monkeypatch):
    client, api = fake_cloud(service, monkeypatch)
    client.restore_share.return_value = {"taskId": "receipt"}
    service.organize = Mock()
    result = service.submit_share(LINK)
    process(service, result)
    job = service.db.get(result["id"])
    assert job["state"] == "saved"
    assert job["payload"]["saved_verified"] is True
    service.organize.assert_called_once()
    assert "temporary-share-access" not in service.db.path.read_bytes().decode("latin1")


def test_job_settings_are_frozen_at_submission(service, monkeypatch):
    fake_cloud(service, monkeypatch)
    result = service.submit_share(LINK)
    service.config.update({"cloud_target": "/Elsewhere"})
    assert service.db.get(result["id"])["payload"]["target"] == "/Media"


def test_upload_verifies_file_and_preserves_local_source(service, monkeypatch):
    source = Path(service.config.snapshot().local_source) / "test.txt"
    source.write_bytes(b"test")
    local = service.storage("local")
    cloud = Mock()
    target = "/uploads/test.txt"
    cloud.item.side_effect = [None, FileItem(path=target, fileid="uploaded", size=4)]
    cloud.ensure.return_value = FileItem(path="/uploads/", type="dir", fileid="upload-dir")
    cloud.api.upload.return_value = FileItem(path=target, fileid="uploaded", size=4)
    monkeypatch.setattr(service, "storage", lambda name: local if name == "local" else cloud)
    monkeypatch.setattr(service, "cloud", lambda: (None, cloud.api))
    result = service.submit_upload([str(source)], "/uploads")
    job = process(service, result)
    assert job["state"] == "completed"
    assert source.read_bytes() == b"test"
    assert service.db.operations(job["id"])[0]["state"] == "completed"


def test_upload_uncertain_result_keeps_running_receipt_for_review(service, monkeypatch):
    source = Path(service.config.snapshot().local_source) / "test.txt"
    source.write_bytes(b"test")
    local = service.storage("local")
    cloud = Mock()
    cloud.item.return_value = None
    cloud.ensure.return_value = FileItem(path="/uploads/", type="dir", fileid="upload-dir")
    cloud.api.upload.return_value = None
    monkeypatch.setattr(service, "storage", lambda name: local if name == "local" else cloud)
    monkeypatch.setattr(service, "cloud", lambda: (None, cloud.api))
    job = process(service, service.submit_upload([str(source)], "/uploads"))
    assert job["state"] == "needs_review"
    assert service.db.operations(job["id"])[0]["state"] == "running"
    assert source.exists()


@pytest.mark.parametrize(
    "link",
    [
        "https://evilguangyapan.com/s/fixture_123",
        "https://guangyapan.com.evil.test/s/fixture_123",
        "http://127.0.0.1/private",
    ],
)
def test_share_parser_only_accepts_correct_domain(link):
    with pytest.raises(ValueError):
        parse_share(link)


@pytest.mark.parametrize(
    "name,path", [("..", "/source/.."), ("sub/file.mkv", "/source/sub/file.mkv"), ("movie.mkv", "/other/movie.mkv")]
)
def test_cloud_children_cannot_escape_selected_directory(name, path):
    from guangya.storage import CloudStorage

    api = Mock()
    api.list_strict.return_value = [FileItem(name=name, path=path, fileid="untrusted")]
    cloud = CloudStorage(api, "/tmp")
    with pytest.raises(ValueError):
        cloud.children(FileItem(path="/source", name="source", type="dir", fileid="folder"))


def test_resending_correct_code_only_requeues_pre_save_failure(service, monkeypatch):
    client, _ = fake_cloud(service, monkeypatch)
    client.share_access.return_value = {}
    result = service.submit_share(LINK)
    job = process(service, result)
    assert job["state"] == "failed"
    retried = service.submit_share(LINK.replace("abcd", "corrected"))
    assert not retried["duplicate"]
    assert retried["id"] == job["id"]
    assert service.db.get(job["id"])["payload"]["code"] == "corrected"
    client.restore_share.assert_not_called()


def test_resending_unknown_save_never_requeues_it(service, monkeypatch):
    client, _ = fake_cloud(service, monkeypatch)
    client.restore_share.side_effect = TimeoutError()
    result = service.submit_share(LINK)
    assert process(service, result)["state"] == "needs_review"
    retried = service.submit_share(LINK.replace("abcd", "corrected"))
    assert retried["duplicate"]
    assert service.db.get(retried["id"])["state"] == "needs_review"
    client.restore_share.assert_called_once()

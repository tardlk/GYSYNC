"""管理鉴权、凭据脱敏和 Bot 用户授权。"""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from guangya.app import create_app
from guangya.config import check_password
from guangya.telegram import Telegram


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "api-config", start_workers=False)
    with TestClient(app) as client:
        yield client, app.state.service, tmp_path / "api-config"


def login(client, directory):
    password = (directory / "admin-password.txt").read_text().strip()
    assert client.post("/api/login", json={"password": password}).status_code == 200


def test_private_api_requires_login_but_health_is_public(client):
    http, service, directory = client
    assert http.get("/health").status_code == 200
    for path in ("/api/config", "/api/jobs", "/api/system"):
        assert http.get(path).status_code == 401
    login(http, directory)
    assert http.get("/api/config").status_code == 200
    assert service.config.path.stat().st_mode & 0o777 == 0o600
    assert check_password(
        (directory / "admin-password.txt").read_text().strip(), service.config.snapshot().password_hash
    )


def test_secrets_are_write_only_and_blank_save_preserves_them(client):
    http, service, directory = client
    login(http, directory)
    response = http.post(
        "/api/config", json={"tmdb_api_key": "fixture-private-key", "telegram_token": "fixture-private-token"}
    )
    assert response.status_code == 200
    assert "fixture-private" not in response.text
    assert response.json()["tmdb_api_key_set"] is True
    http.post("/api/config", json={"tmdb_api_key": ""})
    assert service.config.snapshot().tmdb_api_key == "fixture-private-key"
    assert http.post("/api/config", json={"access_token": "forged"}).status_code == 400


def test_cross_site_post_is_rejected(client):
    http, _, directory = client
    login(http, directory)
    assert (
        http.post("/api/config", headers={"Origin": "https://evil.example"}, json={"scrape": False}).status_code == 403
    )


def test_enabling_bot_requires_whitelist(client):
    http, _, directory = client
    login(http, directory)
    assert http.post("/api/config", json={"telegram_enabled": True, "telegram_token": "fake"}).status_code == 400


def test_bot_ignores_other_users_and_groups(service):
    service.config.update(
        {"telegram_token": "fake", "telegram_user_ids": "123", "telegram_enabled": True}, internal=True
    )
    telegram = Telegram(service)
    telegram.send = Mock()
    service.submit_share = Mock()
    for user, chat in ((12, "private"), (123, "group")):
        telegram.handle(
            {
                "message": {
                    "from": {"id": user},
                    "chat": {"type": chat},
                    "text": "https://guangyapan.com/s/test_share_id",
                }
            }
        )
    service.submit_share.assert_not_called()
    telegram.send.assert_not_called()


def test_bot_share_routes_to_same_service_and_preserves_recipient(service):
    service.config.update(
        {"telegram_token": "fake", "telegram_user_ids": "123", "telegram_enabled": True}, internal=True
    )
    service.submit_share = Mock(return_value={"id": "job", "duplicate": False})
    telegram = Telegram(service)
    telegram.send = Mock()
    text = "https://guangyapan.com/s/test_share_id?code=1234"
    telegram.handle({"message": {"from": {"id": 123}, "chat": {"type": "private"}, "text": text}})
    service.submit_share.assert_called_once_with(text, recipient="123")
    assert telegram.send.call_args.args[0] == "123"


def test_telegram_update_is_durable_and_not_accepted_twice(service):
    assert service.db.accept_update(1234)
    assert not service.db.accept_update(1234)
    service.db.save_value("telegram_offset", 1235)
    assert service.db.value("telegram_offset") == 1235


def test_vendor_has_no_imports_from_live_moviepilot(client):
    import sys

    assert not any(name == "app" or name.startswith("app.") for name in sys.modules)


def test_notification_outbox_survives_bot_offline_and_retries(service):
    result = service.db.create("preview", {}, recipient="123")
    job = service.db.get(result["id"])
    service.state(job, "completed", "完成")
    pending = service.db.pending_notifications()
    assert len(pending) == 1
    telegram = Telegram(service)
    telegram.send = Mock(return_value=False)
    telegram.flush_notifications()
    assert service.db.pending_notifications() == []
    with service.db.connect() as db:
        row = db.execute("SELECT attempts,sent FROM notifications").fetchone()
        assert (row["attempts"], row["sent"]) == (1, 0)
        db.execute("UPDATE notifications SET next_try=0")
    telegram.send.return_value = True
    telegram.flush_notifications()
    with service.db.connect() as db:
        assert db.execute("SELECT sent FROM notifications").fetchone()[0] == 1


def test_qr_login_keeps_device_and_account_tokens_private(client, monkeypatch):
    http, service, directory = client
    login(http, directory)
    fake = Mock()
    fake.get_device_code.return_value = {
        "device_code": "private-device-secret",
        "user_code": "ABCD",
        "verification_uri_complete": "https://www.guangyapan.com/login?code=ABCD",
        "expires_in": 300,
    }
    fake.poll_device_code.return_value = {"access_token": "private-cloud-token", "refresh_token": "private-refresh"}
    monkeypatch.setattr(service, "cloud", lambda **kwargs: (fake, None))
    response = http.post("/api/cloud/login/start", json={})
    assert response.status_code == 200
    assert response.json()["qr_image"].startswith("data:image/svg+xml;base64,")
    assert "private-device-secret" not in response.text
    response = http.post("/api/cloud/login/poll", json={})
    assert response.json()["logged_in"]
    assert "private-cloud-token" not in response.text
    assert service.config.snapshot().access_token == "private-cloud-token"


def test_login_throttles_incorrect_passwords(client):
    http, _, _ = client
    for _ in range(10):
        assert http.post("/api/login", json={"password": "wrong-password"}).status_code == 401
    assert http.post("/api/login", json={"password": "wrong-password"}).status_code == 429


def test_log_formatter_redacts_current_and_rotated_secrets(service):
    import logging
    from guangya.logging import SecretFormatter

    formatter = SecretFormatter(service.config)
    record = logging.LogRecord("upstream", logging.ERROR, "source", 1, "key=fixture-key", (), None)
    assert "fixture-key" not in formatter.format(record)
    service.config.update({"tmdb_api_key": "new-private-key"})
    record.msg = "old=fixture-key new=new-private-key"
    rendered = formatter.format(record)
    assert "fixture-key" not in rendered
    assert "new-private-key" not in rendered


def test_bot_stops_accepting_input_as_soon_as_disabled(service):
    service.config.update(
        {"telegram_token": "fake", "telegram_user_ids": "123", "telegram_enabled": False}, internal=True
    )
    service.submit_share = Mock()
    telegram = Telegram(service)
    telegram.handle(
        {
            "message": {
                "from": {"id": 123},
                "chat": {"type": "private"},
                "text": "https://guangyapan.com/s/fixture_share",
            }
        }
    )
    service.submit_share.assert_not_called()


def test_menu_sync_failure_does_not_block_incoming_messages(service):
    service.config.update(
        {"telegram_token": "fake", "telegram_user_ids": "123", "telegram_enabled": True}, internal=True
    )
    telegram = Telegram(service)
    telegram.register_menu = Mock(side_effect=RuntimeError("menu unavailable"))
    telegram.request = Mock(return_value=[{"update_id": 987, "message": {"text": "/tasks"}}])
    telegram.handle = Mock(side_effect=lambda update: telegram.stop_event.set())
    telegram.run()
    telegram.handle.assert_called_once()
    assert service.db.value("telegram_offset") == 988

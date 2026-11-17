"""独立运行配置；令牌只写入挂载的私有数据目录。"""

import hashlib
import hmac
import os
import secrets
import threading
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

MOVIE_TEMPLATE = "{{title}} ({{year}})/{{title}} ({{year}}){% if part %}-{{part}}{% endif %}{{fileExt}}"
TV_TEMPLATE = "{{title}} ({{year}})/Season {{season}}/{{title}} - {{season_episode}}{% if episode_title %} - {{episode_title}}{% endif %}{{fileExt}}"
MEDIA_EXTENSIONS = (".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mov", ".wmv", ".m4v", ".iso", ".strm")
SUB_EXTENSIONS = (".srt", ".ass", ".ssa", ".sup", ".sub", ".idx")


class Settings(BaseModel):
    """页面可编辑的运行参数；密钥不进入脱敏响应。"""

    model_config = ConfigDict(extra="forbid")
    tmdb_api_key: str = ""
    tmdb_language: str = "zh-CN"
    access_token: str = ""
    refresh_token: str = ""
    client_id: str = "aMe-8VSlkrbQXpUR"
    device_id: str = ""
    telegram_token: str = ""
    telegram_enabled: bool = False
    telegram_user_ids: str = ""
    telegram_offset: int = 0
    cloud_source: str = "/待整理"
    cloud_target: str = "/Media"
    cloud_upload: str = "/Media"
    local_source: str = "/downloads"
    local_target: str = "/media"
    local_roots: list[str] = Field(default_factory=lambda: ["/downloads", "/media"])
    mode: str = "move"
    scrape: bool = True
    type_folders: bool = True
    upload_after_local: bool = False
    min_interval: float = Field(default=0.5, ge=0.5, le=60)
    movie_template: str = MOVIE_TEMPLATE
    tv_template: str = TV_TEMPLATE
    password_hash: str = ""
    secret_key: str = ""


SECRET_FIELDS = {"tmdb_api_key", "access_token", "refresh_token", "telegram_token", "password_hash", "secret_key"}
INTERNAL_FIELDS = {
    "telegram_offset",
    "password_hash",
    "secret_key",
    "access_token",
    "refresh_token",
    "device_id",
    "client_id",
}


def hash_password(password: str) -> str:
    """管理密码使用带盐 PBKDF2，不保存明文到配置。"""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 300000).hex()
    return f"{salt}:{digest}"


def check_password(password: str, encoded: str) -> bool:
    """恒定时间比较管理密码。"""
    try:
        salt, digest = encoded.split(":")
        value = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 300000).hex()
        return hmac.compare_digest(value, digest)
    except ValueError:
        return False


class ConfigStore:
    """单进程配置的原子写入与脱敏投影。"""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "config.json"
        self.lock = threading.RLock()
        self.settings = Settings.model_validate_json(self.path.read_text()) if self.path.exists() else Settings()
        if not self.settings.password_hash:
            password_file = os.getenv("ADMIN_PASSWORD_FILE")
            password = Path(password_file).read_text().strip() if password_file else secrets.token_urlsafe(20)
            self.settings.password_hash = hash_password(password)
            if not password_file:
                self._write(self.directory / "admin-password.txt", password + "\n")
        if not self.settings.secret_key:
            self.settings.secret_key = secrets.token_hex(32)
        self.save()

    @staticmethod
    def _write(path, content):
        temporary = path.with_suffix(path.suffix + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)

    def save(self):
        with self.lock:
            self._write(self.path, self.settings.model_dump_json(indent=2))

    def snapshot(self):
        with self.lock:
            return self.settings.model_copy(deep=True)

    def update(self, values, internal=False):
        with self.lock:
            current = self.settings.model_dump()
            if not internal and set(values) & INTERNAL_FIELDS:
                raise ValueError("不允许修改内部凭据字段")
            for key, value in values.items():
                if key in SECRET_FIELDS and value == "":
                    continue
                current[key] = value
            settings = Settings.model_validate(current)
            if settings.mode not in {"move", "copy", "link"}:
                raise ValueError("整理方式必须为 move/copy/link")
            ids = [v.strip() for v in settings.telegram_user_ids.replace("，", ",").split(",") if v.strip()]
            if any(not v.isdigit() or int(v) <= 0 for v in ids):
                raise ValueError("Telegram 白名单必须为数字 ID")
            if settings.telegram_enabled and (not ids or not settings.telegram_token):
                raise ValueError("启用 Telegram 前需填写 Token 和用户白名单")
            self.settings = settings
            self.save()
            return self.public()

    def public(self):
        values = self.snapshot().model_dump()
        result = {key: value for key, value in values.items() if key not in SECRET_FIELDS | INTERNAL_FIELDS}
        result.update({key + "_set": bool(values[key]) for key in ("tmdb_api_key", "telegram_token", "access_token")})
        return result

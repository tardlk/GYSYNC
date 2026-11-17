"""光鸭存储和任务请求的独立数据模型。"""

from pathlib import PurePosixPath
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class FileItem(BaseModel):
    """云盘条目的稳定身份，不依赖宿主 schemas。"""

    model_config = ConfigDict(extra="allow")
    storage: str = "guangya"
    path: str
    name: str | None = None
    basename: str | None = None
    extension: str | None = None
    type: str = "file"
    fileid: str | None = ""
    parent_fileid: str | None = ""
    size: int | None = 0
    modify_time: int | None = 0
    pickcode: str | None = ""
    drive_id: str | None = ""
    thumbnail: str | None = None

    @model_validator(mode="after")
    def fill_name(self):
        path = PurePosixPath(self.path)
        if not self.name:
            self.name = path.name
        if self.basename is None:
            self.basename = path.stem
        if self.extension is None:
            self.extension = path.suffix.lstrip(".")
        return self


class StorageUsage(BaseModel):
    total: float = 0
    available: float = 0


class OrganizeRequest(BaseModel):
    storage: Literal["local", "guangya"]
    source: str = Field(min_length=1, max_length=2048)
    target: str = Field(min_length=1, max_length=2048)
    mode: Literal["move", "copy", "link"] = "move"
    media_type: Literal["auto", "movie", "tv"] = "auto"
    tmdb_id: int | None = Field(default=None, gt=0)
    scrape: bool = True
    upload_after: bool = False


class ShareRequest(BaseModel):
    text: str = Field(min_length=1, max_length=10000)


class UploadRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=100)
    target: str = Field(min_length=1)


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)

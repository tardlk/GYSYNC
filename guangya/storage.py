"""本地与光鸭存储统一入口，拒绝符号链接越界和目标覆盖。"""

import os
import shutil
from pathlib import Path, PurePosixPath

from guangya.models import FileItem
from guangya.share_protocol import ensure_folder, safe_name


def independent_paths(source, target):
    for raw in (source, target):
        path = PurePosixPath(raw)
        if not raw.startswith("/") or raw.startswith("//") or ".." in path.parts or "\\" in raw or "\x00" in raw:
            raise ValueError("源目录和目标目录必须为绝对路径，不允许 ..")
    left, right = Path(source), Path(target)
    if left == Path("/") or left == right or left in right.parents or right in left.parents:
        raise ValueError("源与目标须独立，源目录不能为根目录")


class LocalStorage:
    """仅能访问明确挂载的本地根目录。"""

    def __init__(self, roots):
        self.roots = [Path(root).resolve() for root in roots]

    def path(self, raw):
        value = Path(raw)
        if not value.is_absolute() or ".." in value.parts:
            raise ValueError("本地路径必须为绝对路径")
        resolved = value.resolve()
        if not any(resolved == root or root in resolved.parents for root in self.roots):
            raise ValueError("本地路径超出允许的挂载目录")
        if any(parent.is_symlink() for parent in (value, *value.parents)):
            raise ValueError("不处理符号链接，请选择真实目录")
        return value

    def item(self, raw):
        path = self.path(raw)
        if not path.exists():
            return None
        stat = path.stat()
        return FileItem(
            storage="local",
            path=str(path),
            name=path.name,
            type="dir" if path.is_dir() else "file",
            size=stat.st_size,
            modify_time=stat.st_mtime_ns,
            fileid=f"{stat.st_dev}:{stat.st_ino}",
        )

    def children(self, folder):
        path = self.path(folder.path)
        return [self.item(str(child)) for child in sorted(path.iterdir()) if not child.is_symlink()]

    def ensure(self, path):
        result = self.path(path)
        result.mkdir(parents=True, exist_ok=True)
        return self.item(str(result))

    def transfer(self, source, target, mode):
        src, dst = self.path(source.path), self.path(target)
        if dst.exists() or dst.is_symlink():
            raise ValueError("目标已存在，保留原文件，未覆盖")
        self.ensure(str(dst.parent))
        if mode == "move":
            # 同文件系统用不覆盖的硬链接+unlink；跨文件系统排他创建目标，复制成功后再删源。
            try:
                os.link(src, dst)
                if not same_file(self.item(str(src)), source.model_dump()):
                    raise ValueError("源文件在移动过程中变化，源路径已保留")
                src.unlink()
            except OSError as error:
                import errno

                if error.errno != errno.EXDEV:
                    raise
                with open(src, "rb") as source_stream, open(dst, "xb") as target_stream:
                    shutil.copyfileobj(source_stream, target_stream, 4 * 1024 * 1024)
                    target_stream.flush()
                    os.fsync(target_stream.fileno())
                if not same_file(self.item(str(src)), source.model_dump()):
                    raise ValueError("源文件在复制过程中变化，源文件已保留")
                shutil.copystat(src, dst)
                if dst.stat().st_size != source.size:
                    raise ValueError("复制后大小不一致，源文件已保留")
                src.unlink()
        elif mode == "link":
            os.link(src, dst)
        else:
            with open(src, "rb") as source_stream, open(dst, "xb") as target_stream:
                shutil.copyfileobj(source_stream, target_stream, 4 * 1024 * 1024)
                target_stream.flush()
                os.fsync(target_stream.fileno())
            if not same_file(self.item(str(src)), source.model_dump()):
                raise ValueError("源文件在复制过程中变化，请核对目标副本")
            shutil.copystat(src, dst)
        return self.item(str(dst))

    def write(self, target, content):
        path = self.path(target)
        self.ensure(str(path.parent))
        try:
            with open(path, "xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            return False
        return True


class CloudStorage:
    """光鸭盘内操作，必须核对 API 返回值和最终条目。"""

    def __init__(self, api, temp):
        self.api, self.temp = api, Path(temp)

    def item(self, path):
        return self.api.get_item_strict(Path(path))

    def children(self, folder):
        children = self.api.list_strict(folder)
        for child in children:
            safe_name(child.name)
            if Path(child.path.rstrip("/")).parent != Path(folder.path):
                raise ValueError("云盘子条目路径超出所选目录")
        return children

    def ensure(self, path):
        if str(path) == "/":
            return self.item("/")
        return ensure_folder(self.api, str(path))

    def transfer(self, source, target, mode):
        if mode == "link":
            raise ValueError("光鸭仅支持移动或复制")
        target = Path(target)
        if self.item(str(target)):
            raise ValueError("目标已存在，未覆盖")
        self.ensure(str(target.parent))
        action = self.api.move if mode == "move" else self.api.copy
        if not action(source, target.parent, new_name=target.name):
            raise RuntimeError("云盘操作未能确认")
        return self.item(str(target))

    def write(self, target, content):
        import tempfile

        target = Path(target)
        if self.item(str(target)):
            return False
        folder = self.ensure(str(target.parent))
        self.temp.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.temp) as directory:
            local = Path(directory) / target.name
            local.write_bytes(content)
            receipt = self.api.upload(folder, local, new_name=target.name)
            if not receipt:
                raise RuntimeError("刮削文件上传失败")
        actual = self.item(str(target))
        if not actual or actual.size != len(content):
            raise RuntimeError("刮削文件尚未确认，请检查云盘")
        return True


def scan(storage, raw, stopped=lambda: False):
    root = storage.item(raw)
    if not root:
        raise ValueError("源路径不存在")
    output, seen = [], set()

    def walk(item, depth=0):
        if stopped():
            raise InterruptedError()
        if depth > 40 or len(output) >= 10000:
            raise ValueError("目录超出处理上限（40 层／10000 项）")
        if item.type != "dir":
            output.append(item)
            return
        if item.fileid in seen:
            raise ValueError("目录身份重复，已停止扫描")
        seen.add(item.fileid)
        for child in storage.children(item):
            if child:
                walk(child, depth + 1)

    walk(root)
    return output


def same_file(item, expected):
    return bool(
        item
        and item.type == "file"
        and item.fileid == expected.get("fileid")
        and item.size == expected.get("size")
        and item.modify_time == expected.get("modify_time")
    )

"""独立任务编排：串行转存、预览、文件执行、上传与可核对的持久回执。"""

import copy
import hashlib
import fcntl
import logging
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

from guangya.cloud_api import GuangYaApi
from guangya.cloud_client import GuangYaClient, RequestGate
from guangya.config import ConfigStore, MEDIA_EXTENSIONS, SUB_EXTENSIONS
from guangya.db import Database
from guangya.media import MediaEngine, ReviewRequired
from guangya.models import OrganizeRequest
from guangya.share_protocol import parse_share, read_manifest, saved_manifest, ensure_folder
from guangya.storage import CloudStorage, LocalStorage, independent_paths, same_file, scan

logger = logging.getLogger("guangya")


class Service:
    """拥有服务生命周期和副作用；单 worker 持有跨进程文件锁。"""

    def __init__(self, directory):
        self.config = ConfigStore(directory)
        self.db = Database(Path(directory) / "jobs.db")
        self.media = MediaEngine(self.config, self.db)
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.io_lock = threading.RLock()
        self.gate = RequestGate()
        self.client = None
        self.api = None
        self.client_identity = None
        self.worker = None
        self.telegram = None

    def start(self):
        self.lock_file = open(self.config.directory / "worker.lock", "a+")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close()
            raise RuntimeError("已有服务进程运行，拒绝启动第二个 worker/Bot") from None
        self.db.recover()
        self.worker = threading.Thread(target=self.run, name="organizer-worker", daemon=True)
        self.worker.start()
        from guangya.telegram import Telegram

        self.telegram = Telegram(self)
        self.telegram.start()

    def close(self):
        self.stop_event.set()
        self.wake.set()
        if self.telegram:
            self.telegram.close()
        if self.worker:
            self.worker.join(10)

    def cloud(self, login_required=True):
        config = self.config.snapshot()
        if login_required and not config.access_token:
            raise ValueError("请先扫码登录光鸭")
        identity = (config.access_token, config.refresh_token, config.device_id, config.min_interval)
        if identity != self.client_identity:
            self.gate.interval = config.min_interval

            def refresh(access, refresh_token):
                self.config.update({"access_token": access, "refresh_token": refresh_token}, internal=True)
                self.client_identity = None

            self.client = GuangYaClient(
                access_token=config.access_token,
                refresh_token=config.refresh_token,
                client_id=config.client_id,
                device_id=config.device_id,
                request_gate=self.gate,
                on_token_refresh=refresh,
            )
            if self.client.device_id != config.device_id:
                self.config.update({"device_id": self.client.device_id}, internal=True)
            self.api = GuangYaApi(self.client, "guangya", resume_store_dir=str(self.config.directory / "upload-resume"))
            self.client_identity = identity
        return self.client, self.api

    def storage(self, name):
        if name == "local":
            return LocalStorage(self.config.snapshot().local_roots)
        if name != "guangya":
            raise ValueError("不支持的存储")
        return CloudStorage(self.cloud()[1], self.config.directory / "tmp")

    def options(self):
        config = self.config.snapshot()
        return {
            key: getattr(config, key)
            for key in ("movie_template", "tv_template", "type_folders", "scrape", "upload_after_local", "cloud_upload")
        }

    def submit_share(self, text, recipient=None):
        share_id, code = parse_share(text)
        config = self.config.snapshot()
        independent_paths(config.cloud_source, config.cloud_target)
        self.cloud()
        import uuid

        job_id = uuid.uuid4().hex[:12]
        payload = {
            "share_id": share_id,
            "code": code,
            "source": config.cloud_source + "/" + job_id,
            "target": config.cloud_target,
            "storage": "guangya",
            "mode": config.mode if config.mode != "link" else "move",
            "scrape": config.scrape,
            "media_type": "auto",
            "settings": self.options(),
        }
        result = self.db.create("share", payload, dedupe="share:" + share_id, recipient=recipient, job_id=job_id)
        if result.get("duplicate") and self.db.retry_failed_share(result["id"], code):
            result.update(state="queued", duplicate=False)
        self.wake.set()
        return result

    def submit_organize(self, request, preview=True, recipient=None):
        request = OrganizeRequest.model_validate(request).model_dump()
        independent_paths(request["source"], request["target"])
        if request["storage"] == "local":
            local = self.storage("local")
            local.path(request["source"])
            local.path(request["target"])
        elif request["mode"] == "link":
            raise ValueError("光鸭不支持硬链接")
        request["settings"] = self.options()
        result = self.db.create("preview" if preview else "organize", request, recipient=recipient)
        self.wake.set()
        return result

    def submit_upload(self, paths, target, recipient=None):
        independent_paths("/__upload_source__", target)
        local = self.storage("local")
        for raw in paths:
            if not local.item(raw):
                raise ValueError("上传源不存在")
        self.cloud()
        result = self.db.create("upload", {"paths": paths, "target": target}, recipient=recipient)
        self.wake.set()
        return result

    def execute_preview(self, job_id):
        job = self.db.get(job_id)
        if job["kind"] != "preview" or job["state"] != "preview_ready":
            raise ValueError("只有有效预览可执行；请先检查任务结果")
        result = job["result"]
        if not result.get("plans") or result.get("errors"):
            raise ValueError("预览有未解决的问题，请指定媒体并重新预览")
        with self.db.connect() as db:
            cursor = db.execute(
                "UPDATE jobs SET kind='approved',state='queued',message='预览已确认，等待执行' WHERE id=? AND state='preview_ready'",
                (job_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError("任务已被执行或状态已变化")
        self.wake.set()
        return self.db.public(self.db.get(job_id))

    def retry(self, job_id, tmdb_id=None, media_type=None):
        job = self.db.get(job_id)
        payload = copy.deepcopy(job["payload"])
        operations = self.db.operations(job_id)
        if any(op["state"] in {"running", "needs_review"} for op in operations):
            raise ValueError("存在结果不明的文件操作，请先在存储中核对，不能自动重试")
        if job["state"] not in {"failed", "needs_review", "partial"}:
            raise ValueError("该任务当前不需要重试")
        if job["kind"] == "share" and not payload.get("saved_verified"):
            raise ValueError("转存结果需要先核对；不会重新提交转存请求")
        if tmdb_id:
            if media_type not in {"movie", "tv"}:
                raise ValueError("请指定电影或电视剧类型")
            payload.update(tmdb_id=int(tmdb_id), media_type=media_type)
        if job["kind"] == "upload":
            raise ValueError("上传请检查目标目录后重新提交，避免重复上传")
        # 成功的移动文件不再存在于源目录，其余文件重新生成只读预览。
        payload["settings"] = self.options()
        result = self.db.create("preview", payload, recipient=job["recipient"])
        self.wake.set()
        return result

    def notify(self, job, message):
        """通知持久入队，Telegram 故障不阻塞云盘或本地文件操作。"""
        if job.get("recipient"):
            key = job["id"] + ":" + job["state"] + ":" + hashlib.sha256(message.encode()).hexdigest()[:12]
            self.db.queue_notification(key, job["recipient"], f"任务 {job['id']}\n{message}")

    def state(self, job, state, message, result=None):
        self.db.set(job["id"], state, message, result=result)
        job["state"] = state
        logger.info("任务 %s: %s", job["id"], state)
        if state in {"saved", "completed", "partial", "failed", "needs_review", "preview_ready"}:
            self.notify(job, message)

    def run(self):
        try:
            while not self.stop_event.is_set():
                job = self.db.next()
                if not job:
                    self.wake.wait(3)
                    self.wake.clear()
                    continue
                self.process(job)
        finally:
            self.lock_file.close()

    def process(self, job):
        """处理一个持久任务；不可确认的写入一律暂停。"""
        try:
            with self.io_lock:
                if job["kind"] == "share" and not job["payload"].get("saved_verified"):
                    self.save_share(job)
                if self.stop_event.is_set():
                    return
                if job["kind"] == "upload":
                    self.upload(job)
                else:
                    self.organize(job)
        except InterruptedError:
            if job["state"] in {"saving", "confirming", "executing", "uploading"}:
                self.state(job, "needs_review", "运行停止，写入结果需人工核对")
            elif job["state"] == "planning":
                self.state(job, "queued", "规划已停止，可在重启后继续")
        except Exception as error:
            uncertain = job["state"] in {"saving", "confirming", "executing", "uploading"}
            message = str(error) if isinstance(error, ValueError) else type(error).__name__
            for key in ("tmdb_api_key", "access_token", "refresh_token", "telegram_token"):
                secret = getattr(self.config.snapshot(), key)
                if secret:
                    message = message.replace(secret, "[已隐藏]")
            self.state(
                job, "needs_review" if uncertain or isinstance(error, ReviewRequired) else "failed", message[:500]
            )

    def save_share(self, job):
        payload = job["payload"]
        client, api = self.cloud()
        self.state(job, "inspecting", "读取分享清单")
        token = client.share_access(payload["share_id"], payload["code"]).get("accessToken")
        if not token:
            raise ValueError("无法访问分享，请检查链接和提取码")
        roots, manifest = read_manifest(client, token, self.stop_event.is_set)
        folder = ensure_folder(api, payload["source"])
        if api.list_strict(folder):
            raise ReviewRequired("任务源目录已有内容，请核对后手动整理")
        self.state(job, "saving", "转存写入中，不会自动重放")
        for offset in range(0, len(roots), 20):
            if self.stop_event.is_set():
                raise InterruptedError()
            receipt = client.restore_share(token, roots[offset : offset + 20], folder.fileid)
            payload["restore_task"] = receipt.get("taskId")
            self.db.set(job["id"], "confirming", "核对完整转存清单", payload=payload)
            job["state"] = "confirming"
        deadline = time.monotonic() + 180
        while saved_manifest(api, folder, self.stop_event.is_set) != manifest:
            if time.monotonic() > deadline:
                raise ReviewRequired("转存内容未完整确认，请先核对源目录")
            if self.stop_event.wait(3):
                raise InterruptedError()
        payload["saved_verified"] = True
        payload["saved_files"] = sum(item["type"] == "file" for item in manifest.values())
        self.db.set(job["id"], "saved", "转存清单已完整核对", payload=payload)
        self.state(job, "saved", f"转存完成，共 {payload['saved_files']} 个文件，开始识别")

    def build_plans(self, job, storage):
        payload = job["payload"]
        self.state(job, "planning", "使用 MP 引擎识别与规划")
        items = scan(storage, payload["source"], self.stop_event.is_set)
        videos = [item for item in items if Path(item.path).suffix.lower() in MEDIA_EXTENSIONS]
        if not videos:
            raise ReviewRequired("源目录中没有支持的媒体文件")
        plans, errors, targets = [], [], set()
        for index, item in enumerate(videos):
            if self.stop_event.is_set():
                raise InterruptedError()
            try:
                plan = self.media.plan(item, payload, payload["settings"])
                if plan["target"] in targets or storage.item(plan["target"]):
                    raise ReviewRequired("目标存在或多文件产生同名目标，请调整模板或手工处理")
                targets.add(plan["target"])
                plans.append(plan)
                source_path = Path(item.path)
                for extra in items:
                    path = Path(extra.path)
                    if path.parent != source_path.parent or path.suffix.lower() not in SUB_EXTENSIONS:
                        continue
                    if path.stem != source_path.stem and not path.stem.startswith(source_path.stem + "."):
                        continue
                    suffix = path.name[len(source_path.stem) :]
                    target = str(Path(plan["target"]).with_suffix("")) + suffix
                    if target in targets or storage.item(target):
                        raise ReviewRequired("字幕目标同名冲突")
                    targets.add(target)
                    plans.append(
                        {
                            "source": extra.path,
                            "target": target,
                            "identity": extra.model_dump(),
                            "title": plan["title"],
                            "role": "subtitle",
                            "state": "planned",
                            "tmdb_id": plan["tmdb_id"],
                        }
                    )
            except (ValueError, ReviewRequired) as error:
                errors.append({"source": item.path, "message": str(error)})
            self.db.set(
                job["id"], "planning", f"识别 {index + 1}/{len(videos)}", result={"plans": plans, "errors": errors}
            )
        return plans, errors

    def organize(self, job):
        payload = job["payload"]
        storage = self.storage(payload["storage"])
        if job["kind"] == "approved":
            plans, errors = job["result"]["plans"], []
        else:
            plans, errors = self.build_plans(job, storage)
        if errors:
            self.state(
                job,
                "needs_review",
                "识别或规划存在问题，未执行文件操作；请查看结果并指定媒体",
                {"plans": plans, "errors": errors},
            )
            return
        if job["kind"] == "preview":
            self.state(job, "preview_ready", f"预览完成，{len(plans)} 项待确认", {"plans": plans, "errors": []})
            return
        self.execute(job, storage, plans)

    def execute(self, job, storage, plans):
        payload = job["payload"]
        # 执行前重新核对所有身份，避免部分执行后才发现预览已过期。
        for plan in plans:
            if not same_file(storage.item(plan["source"]), plan["identity"]):
                raise ReviewRequired("源文件在预览后变化，请重新预览")
            if storage.item(plan["target"]):
                raise ReviewRequired("目标在预览后出现文件，请重新预览")
        self.state(job, "executing", "逐文件执行，保留持久操作回执")
        done, metadata_errors = 0, []
        metadata_paths = set()
        for plan in plans:
            if self.stop_event.is_set():
                raise InterruptedError()
            self.db.operation(job["id"], plan["source"], plan["target"], "running", plan)
            source = storage.item(plan["source"])
            if not same_file(source, plan["identity"]):
                raise ReviewRequired("执行前源身份发生变化")
            target = storage.transfer(source, plan["target"], payload["mode"])
            if (
                not target
                or target.size != source.size
                or (payload["mode"] == "move" and target.fileid != source.fileid and payload["storage"] == "guangya")
            ):
                raise ReviewRequired("目标身份或大小未确认")
            if payload["mode"] == "move" and storage.item(plan["source"]):
                raise ReviewRequired("源文件仍存在，移动结果未确认")
            self.db.operation(job["id"], plan["source"], plan["target"], "completed", {"target": target.model_dump()})
            plan["state"] = "completed"
            done += 1
            self.db.set(
                job["id"], "executing", f"已完成 {done}/{len(plans)}", result={"plans": plans, "completed": done}
            )
            if plan["role"] == "media" and payload.get("scrape"):
                try:
                    documents, images = self.media.scrape(plan)
                    for path, content in documents.items():
                        if storage.write(path, content):
                            metadata_paths.add(path)
                    for path, url in images.items():
                        if storage.item(path):
                            continue
                        parsed = urlsplit(url)
                        if parsed.scheme != "https" or parsed.hostname != "image.tmdb.org":
                            continue
                        http = requests.Session()
                        http.trust_env = False
                        with http.get(url, timeout=30, stream=True) as response:
                            response.raise_for_status()
                            content = bytearray()
                            for block in response.iter_content(65536):
                                content.extend(block)
                                if len(content) > 25 * 1024 * 1024:
                                    raise ValueError("刮削图片超过 25MB")
                        http.close()
                        if storage.write(path, bytes(content)):
                            metadata_paths.add(path)
                except Exception as error:
                    metadata_errors.append(
                        {"source": plan["source"], "message": "媒体已整理，刮削未完成：" + type(error).__name__}
                    )
        upload_errors = []
        if payload["storage"] == "local" and payload.get("upload_after"):
            try:
                # 同步上传已整理的媒体、字幕与同目录刮削文件；上传失败不回滚本地成品。
                paths = sorted({plan["target"] for plan in plans} | metadata_paths)
                upload_job = self.db.get(job["id"])
                upload_job["payload"] = {
                    "paths": paths,
                    "target": payload["settings"]["cloud_upload"],
                    "relative_root": payload["target"],
                }
                self.upload(upload_job, finalize=False)
            except Exception as error:
                upload_errors.append({"message": "本地整理完成，上传未完成：" + type(error).__name__})
        errors = metadata_errors + upload_errors
        self.state(
            job,
            "partial" if errors else "completed",
            f"文件整理完成 {done}/{len(plans)}" + ("；有刮削或上传问题，请检查记录" if errors else ""),
            {"plans": plans, "completed": done, "errors": errors},
        )

    def upload(self, job, finalize=True):
        self.state(job, "uploading", "准备上传；已存在的同名文件会跳过，源文件保留")
        payload = job["payload"]
        local = self.storage("local")
        cloud = self.storage("guangya")
        records = []
        for raw in payload["paths"]:
            files = scan(local, raw, self.stop_event.is_set)
            for item in files:
                if self.stop_event.is_set():
                    raise InterruptedError()
                relative = Path(item.path).relative_to(Path(payload.get("relative_root") or Path(raw).parent))
                target = str(Path(payload["target"]) / relative)
                existing = cloud.item(target)
                if existing:
                    records.append(
                        {"source": item.path, "target": target, "state": "skipped", "message": "同名目标已存在，未覆盖"}
                    )
                    continue
                self.db.operation(job["id"], item.path, target, "running", item.model_dump())
                folder = cloud.ensure(str(Path(target).parent))
                result_meta = {}
                result = cloud.api.upload(
                    folder,
                    Path(item.path),
                    new_name=Path(target).name,
                    result_meta=result_meta,
                    on_phase=lambda phase: self.db.set(job["id"], "uploading", phase + ": " + item.name),
                )
                actual = cloud.item(target)
                if not result or not actual or actual.size != item.size:
                    raise ReviewRequired("上传结果未完整确认，源文件保留，请检查目标目录")
                self.db.operation(
                    job["id"],
                    item.path,
                    target,
                    "completed",
                    {"target": actual.model_dump(), "flash": result_meta.get("flash", False)},
                )
                records.append(
                    {
                        "source": item.path,
                        "target": target,
                        "state": "completed",
                        "flash": result_meta.get("flash", False),
                    }
                )
                self.db.set(job["id"], "uploading", f"已处理 {len(records)} 个文件", result={"items": records})
        if finalize:
            self.state(job, "completed", "上传处理完成，源文件均保留", {"items": records})
        return records

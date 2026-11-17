"""FastAPI 管理接口、私有登录会话与单 worker 生命周期。"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from guangya.config import check_password
from guangya.models import LoginRequest, OrganizeRequest, ShareRequest, UploadRequest
from guangya.service import Service

VERSION = "0.1.0"


def create_app(directory=None, start_workers=True):
    """用隔离配置目录创建应用；测试时可关闭外部任务线程。"""
    directory = Path(directory or os.getenv("DATA_DIR", "/config"))
    service = Service(directory)
    attempts = {}
    attempt_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        if start_workers:
            service.start()
        yield
        service.close()

    application = FastAPI(title="光鸭整理", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None)
    application.state.service = service

    @application.middleware("http")
    async def same_origin(request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "拒绝跨站请求"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' https://image.tmdb.org data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'"
        )
        return response

    def signature(value):
        return hmac.new(service.config.snapshot().secret_key.encode(), value.encode(), hashlib.sha256).hexdigest()

    def authenticate(request: Request):
        token = request.cookies.get("organizer_session", "")
        authorization = request.headers.get("authorization", "")
        if authorization.startswith("Bearer "):
            token = authorization[7:]
        try:
            payload, signed = token.rsplit(".", 1)
            if not hmac.compare_digest(signature(payload), signed):
                raise ValueError()
            expires = int(payload.split(":", 1)[0])
            if expires < time.time():
                raise ValueError()
        except ValueError, TypeError:
            raise HTTPException(401, "请先登录管理页面") from None
        return True

    @application.exception_handler(ValueError)
    async def invalid(_request, error):
        return JSONResponse({"detail": str(error)}, status_code=400)

    @application.exception_handler(Exception)
    async def unexpected(_request, error):
        logging.getLogger("guangya").error("接口失败：%s", type(error).__name__)
        return JSONResponse({"detail": "请求未能确认，请检查任务记录与日志"}, status_code=500)

    @application.get("/health")
    def health():
        if start_workers and service.worker and not service.worker.is_alive():
            raise HTTPException(503, "任务进程未运行")
        return {"status": "ok", "version": VERSION}

    @application.post("/api/login")
    def login(payload: LoginRequest, request: Request, response: Response):
        host = request.client.host if request.client else "unknown"
        with attempt_lock:
            recent = [stamp for stamp in attempts.get(host, []) if stamp > time.time() - 300]
            attempts[host] = recent
            if len(recent) >= 10:
                raise HTTPException(429, "登录失败次数较多，请五分钟后重试")
            if not check_password(payload.password, service.config.snapshot().password_hash):
                attempts[host].append(time.time())
                raise HTTPException(401, "管理密码错误")
            attempts.pop(host, None)
        value = f"{int(time.time() + 86400)}:{secrets.token_hex(16)}"
        token = value + "." + signature(value)
        response.set_cookie(
            "organizer_session",
            token,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            max_age=86400,
        )
        return {"success": True}

    @application.post("/api/logout")
    def logout(response: Response, _=Depends(authenticate)):
        response.delete_cookie("organizer_session")
        return {"success": True}

    @application.get("/api/config")
    def config(_=Depends(authenticate)):
        return service.config.public()

    @application.post("/api/config")
    def save_config(payload: dict, _=Depends(authenticate)):
        return service.config.update(payload)

    @application.get("/api/jobs")
    def jobs(_=Depends(authenticate)):
        return {"items": service.db.list()}

    @application.get("/api/jobs/{job_id}")
    def job(job_id: str, _=Depends(authenticate)):
        result = service.db.public(service.db.get(job_id))
        result["operations"] = service.db.operations(job_id)
        return result

    @application.post("/api/share")
    def share(payload: ShareRequest, _=Depends(authenticate)):
        return service.submit_share(payload.text)

    @application.post("/api/organize/preview")
    def preview(payload: OrganizeRequest, _=Depends(authenticate)):
        return service.submit_organize(payload.model_dump())

    @application.post("/api/jobs/{job_id}/execute")
    def execute(job_id: str, _=Depends(authenticate)):
        return service.execute_preview(job_id)

    @application.post("/api/jobs/{job_id}/retry")
    def retry(job_id: str, payload: dict, _=Depends(authenticate)):
        return service.retry(job_id, payload.get("tmdb_id"), payload.get("media_type"))

    @application.post("/api/upload")
    def upload(payload: UploadRequest, _=Depends(authenticate)):
        return service.submit_upload(payload.paths, payload.target)

    @application.get("/api/files")
    def files(storage: str, path: str, _=Depends(authenticate)):
        with service.io_lock:
            provider = service.storage(storage)
            item = provider.item(path)
            if not item or item.type != "dir":
                raise ValueError("目录不存在")
            return {"path": path, "items": [child.model_dump() for child in provider.children(item)]}

    @application.post("/api/cloud/login/start")
    def start_login(_=Depends(authenticate)):
        with service.io_lock:
            client, _api = service.cloud(login_required=False)
            data = client.get_device_code()
            if not data or not data.get("device_code"):
                raise ValueError("获取登录二维码失败")
            application.state.device_code = data["device_code"]
            application.state.device_expiry = time.time() + int(data.get("expires_in") or 300)
            public = {
                key: data.get(key)
                for key in ("user_code", "verification_uri", "verification_uri_complete", "expires_in")
            }
            url = data.get("verification_uri_complete") or data.get("verification_uri")
            if url:
                import base64
                import io
                import qrcode
                from qrcode.image.svg import SvgPathImage

                image = qrcode.make(url, image_factory=SvgPathImage)
                buffer = io.BytesIO()
                image.save(buffer)
                public["qr_image"] = "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode()
            return public

    @application.post("/api/cloud/login/poll")
    def poll_login(_=Depends(authenticate)):
        if not getattr(application.state, "device_code", None) or time.time() > application.state.device_expiry:
            raise ValueError("登录码已过期，请重新获取")
        with service.io_lock:
            client, _api = service.cloud(login_required=False)
            result = client.poll_device_code(application.state.device_code)
            if not result or result.get("waiting") or not result.get("access_token"):
                return {"logged_in": False}
            service.config.update(
                {"access_token": result["access_token"], "refresh_token": result.get("refresh_token", "")},
                internal=True,
            )
            application.state.device_code = None
            return {"logged_in": True}

    @application.get("/api/cloud/account")
    def account(_=Depends(authenticate)):
        with service.io_lock:
            client, _api = service.cloud()
            user = client.get_user_info()
            usage = _api.usage()
            return {
                "logged_in": True,
                "name": user.get("nickname") or user.get("name") or "",
                "total": usage.total if usage else 0,
                "available": usage.available if usage else 0,
            }

    @application.post("/api/tmdb/test")
    def tmdb_test(_=Depends(authenticate)):
        from guangya.mp_core.schemas.types import MediaType

        data = service.media.api().get_info(MediaType.MOVIE, 27205, raise_on_connection_error=True)
        if not data:
            raise ValueError("TMDB 请求未成功，请检查 Key 和网络")
        return {"success": True, "title": data.get("title") or data.get("name")}

    @application.get("/api/system")
    def system(_=Depends(authenticate)):
        import importlib.metadata

        return {
            "version": VERSION,
            "upstream": json.loads((Path(__file__).resolve().parent / "upstream.json").read_text()),
            "dependencies": len(list(importlib.metadata.distributions())),
            "worker_alive": bool(service.worker and service.worker.is_alive()),
            "telegram_alive": bool(service.telegram and service.telegram.thread and service.telegram.thread.is_alive()),
        }

    static = Path(__file__).parent / "web"
    application.mount("/static", StaticFiles(directory=static), name="static")

    @application.get("/")
    def index():
        return FileResponse(static / "index.html")

    return application


def main():
    """单进程部署入口，避免多个 worker 重复轮询。"""
    import uvicorn

    from guangya.logging import SecretFormatter

    application = create_app()
    handler = logging.StreamHandler()
    handler.setFormatter(SecretFormatter(application.state.service.config))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    uvicorn.run(
        application,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8796")),
        workers=1,
        access_log=False,
    )


if __name__ == "__main__":
    main()

"""独立 Telegram Bot：白名单私聊、长轮询、任务命令和菜单同步。"""

import logging
import shlex
import threading
import time

import requests

from guangya.share_protocol import allowed_user

logger = logging.getLogger("guangya.telegram")
COMMANDS = [
    {"command": "guangya", "description": "保存光鸭分享并整理"},
    {"command": "tasks", "description": "查看最近任务"},
    {"command": "retry", "description": "指定媒体并重新预览"},
    {"command": "confirm", "description": "执行已确认的整理预览"},
    {"command": "organize_local", "description": "预览本地源目录整理"},
    {"command": "organize_cloud", "description": "预览光鸭源目录整理"},
    {"command": "upload", "description": "上传本地文件或目录"},
    {"command": "help", "description": "使用帮助"},
]
HELP = """光鸭整理独立服务
直接发送光鸭分享链接，可附“提取码：1234”，会自动保存并整理。
/guangya 分享链接 提取码：1234
/tasks 查看最近任务
/retry 任务ID movie或tv TMDB数字ID：指定媒体并生成新预览
/confirm 预览任务ID：执行预览
/organize_local：预览已配置的本地源目录
/organize_cloud：预览已配置的光鸭源目录
/upload "本地绝对路径"：上传到已配置的光鸭上传目录
同名目标保留，结果不明时暂停；服务只处理白名单用户的私聊。"""


class Telegram:
    """只有主 worker 进程启动长轮询，保存 offset 避免重启重复执行。"""

    def __init__(self, service):
        self.service = service
        self.stop_event = threading.Event()
        self.thread = None
        self.menu_token = None
        self.menu_retry_at = 0
        self.http = requests.Session()
        self.http.trust_env = False

    def start(self):
        self.thread = threading.Thread(target=self.run, name="telegram-poll", daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(2)
        self.http.close()

    def request(self, method, payload, token=None, timeout=20):
        token = token or self.service.config.snapshot().telegram_token
        if not token:
            raise ValueError("未设置 Bot Token")
        try:
            response = self.http.post(
                "https://api.telegram.org/bot" + token + "/" + method, json=payload, timeout=(5, timeout)
            )
            data = response.json()
        except requests.RequestException:
            self.http.close()
            self.http = requests.Session()
            self.http.trust_env = False
            raise RuntimeError("Telegram 网络请求失败") from None
        except ValueError:
            raise RuntimeError("Telegram 响应格式异常") from None
        if not data.get("ok"):
            code = data.get("error_code")
            if code == 409:
                raise RuntimeError("Bot 正被其他程序轮询，请关闭旧服务的 Bot 通道")
            raise RuntimeError(f"Telegram API 错误 {code}")
        return data["result"]

    def send(self, user, text):
        config = self.service.config.snapshot()
        if not config.telegram_enabled or not allowed_user(user, config.telegram_user_ids):
            return False
        try:
            self.request(
                "sendMessage",
                {"chat_id": str(user), "text": text[:4000], "link_preview_options": {"is_disabled": True}},
            )
            return True
        except Exception as error:
            logger.warning("Telegram 通知未确认：%s", type(error).__name__)
            return False

    def register_menu(self, token):
        self.request("setMyCommands", {"commands": COMMANDS}, token=token)
        # 清理本服务已知语言列表，回退到统一新菜单。
        for language in ("zh", "en"):
            self.request("deleteMyCommands", {"language_code": language}, token=token)
        self.request("setChatMenuButton", {"menu_button": {"type": "commands"}}, token=token)
        self.menu_token = token

    def flush_notifications(self):
        for notification in self.service.db.pending_notifications():
            if self.stop_event.is_set():
                break
            delivered = self.send(notification["recipient"], notification["text"])
            self.service.db.notification_result(notification["id"], delivered)

    def run(self):
        while not self.stop_event.is_set() and not self.service.stop_event.is_set():
            config = self.service.config.snapshot()
            if not config.telegram_enabled or not config.telegram_token:
                self.stop_event.wait(2)
                continue
            try:
                if self.menu_token != config.telegram_token and time.monotonic() >= self.menu_retry_at:
                    try:
                        self.register_menu(config.telegram_token)
                    except ValueError, RuntimeError:
                        logger.warning("菜单同步暂不可用，稍后重试；继续接收消息")
                        self.menu_retry_at = time.monotonic() + 60
                self.flush_notifications()
                offset = self.service.db.value("telegram_offset", config.telegram_offset)
                updates = self.request(
                    "getUpdates", {"offset": offset, "timeout": 10, "allowed_updates": ["message"]}, timeout=20
                )
                for update in updates:
                    if self.stop_event.is_set():
                        break
                    update_id = update.get("update_id")
                    if update_id is None:
                        continue
                    # 先持久接收去重。若提交中断，不重放写操作；用户可按任务记录核对。
                    if self.service.db.accept_update(update_id):
                        self.handle(update)
                    self.service.db.save_value("telegram_offset", update_id + 1)
            except Exception as error:
                logger.warning("Telegram 接收暂不可用：%s", str(error))
                self.stop_event.wait(5)

    def handle(self, update):
        message = update.get("message") or {}
        user = str(message.get("from", {}).get("id") or "")
        config = self.service.config.snapshot()
        if not config.telegram_enabled:
            return
        if message.get("chat", {}).get("type") != "private" or not allowed_user(user, config.telegram_user_ids):
            return
        text = message.get("text") or ""
        command = text.split(maxsplit=1)[0].split("@", 1)[0] if text.strip() else ""
        argument = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
        try:
            if command in {"/start", "/help"}:
                self.send(user, HELP)
                return
            if command == "/tasks":
                jobs = self.service.db.list(8)
                self.send(
                    user,
                    "最近任务：\n" + "\n".join(f"{job['id']} {job['state']} {job['message'][:100]}" for job in jobs),
                )
                return
            if command == "/confirm":
                result = self.service.execute_preview(argument.strip())
            elif command == "/retry":
                parts = argument.split()
                if len(parts) != 3:
                    raise ValueError("用法：/retry 任务ID movie或tv TMDB数字ID")
                result = self.service.retry(parts[0], int(parts[2]), parts[1])
            elif command in {"/organize_local", "/organize_cloud"}:
                local = command == "/organize_local"
                result = self.service.submit_organize(
                    {
                        "storage": "local" if local else "guangya",
                        "source": config.local_source if local else config.cloud_source,
                        "target": config.local_target if local else config.cloud_target,
                        "mode": config.mode,
                        "scrape": config.scrape,
                        "upload_after": config.upload_after_local if local else False,
                    },
                    recipient=user,
                )
            elif command == "/upload":
                paths = shlex.split(argument)
                if not paths:
                    raise ValueError('用法：/upload "本地文件或目录绝对路径"')
                result = self.service.submit_upload(paths, config.cloud_upload, recipient=user)
            elif command == "/guangya" or "guangyapan.com" in text:
                result = self.service.submit_share(argument if command == "/guangya" else text, recipient=user)
            else:
                self.send(user, "发送光鸭分享链接即可，或使用 /help 查看命令。")
                return
            self.send(user, ("已有任务，不会重复提交：" if result.get("duplicate") else "任务已接收：") + result["id"])
        except (ValueError, RuntimeError) as error:
            self.send(user, str(error))

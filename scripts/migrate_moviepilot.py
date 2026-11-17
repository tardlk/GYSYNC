"""从本机 MP 配置只读迁移登录、Telegram 设置和已有分享去重记录。"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from guangya.config import ConfigStore
from guangya.db import Database

parser = argparse.ArgumentParser()
parser.add_argument("--source-config", type=Path, required=True)
parser.add_argument("--target-config", type=Path, required=True)
args = parser.parse_args()
source = sqlite3.connect("file:" + str(args.source_config / "user.db") + "?mode=ro", uri=True)
row = source.execute("SELECT config_data FROM plugininstance WHERE instance_id='guangyasync'").fetchone()
if not row:
    raise SystemExit("未找到光鸭同步插件配置")
plugin = json.loads(row[0])
store = ConfigStore(args.target_config)
values = {key: plugin.get(key, "") for key in ("access_token", "refresh_token", "device_id", "client_id")}
values["telegram_user_ids"] = plugin.get("telegram_user_ids", "")
row = source.execute("SELECT value FROM systemconfig WHERE key='Notifications'").fetchone()
if row:
    channels = json.loads(row[0])
    telegram = next((item for item in channels if item.get("config", {}).get("TELEGRAM_TOKEN")), None)
    if telegram:
        values["telegram_token"] = telegram["config"]["TELEGRAM_TOKEN"]
values["telegram_enabled"] = False
store.update(values, internal=True)
db = Database(args.target_config / "jobs.db")
count = 0
for file in args.source_config.rglob("share_jobs.json"):
    for job in json.loads(file.read_text()):
        # 旧服务写入中的任务不恢复执行；仅迁移去重与源路径，避免再次转存。
        settings = job.get("settings", {})
        payload = {
            "source": job["staging_dir"],
            "target": settings.get("share_target_dir", "/Media"),
            "storage": "guangya",
            "mode": settings.get("share_transfer_type", "move"),
            "scrape": settings.get("share_scrape", False),
            "media_type": "auto",
            "settings": {
                key: getattr(store.snapshot(), key)
                for key in (
                    "movie_template",
                    "tv_template",
                    "type_folders",
                    "scrape",
                    "upload_after_local",
                    "cloud_upload",
                )
            },
            "saved_verified": job["state"] in {"saved", "organizing", "completed", "partial", "needs_review"},
        }
        result = db.create("legacy_share", payload, dedupe="share:" + job["share_id"], state="needs_review")
        if result.get("duplicate"):
            continue
        db.set(
            result["id"],
            "needs_review",
            "从 MP 迁移的既有任务；" + job.get("message", "请核对资源"),
            result={"legacy_job_id": job["id"]},
        )
        count += 1
print(f"账号和 Bot 凭据已迁移；迁移 {count} 条既有分享记录；Telegram 保持关闭，接管前请停用 MP 对应通道。")

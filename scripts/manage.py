"""本机开发服务启动/停止；Docker 使用镜像自带的前台入口。"""

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("action", choices=["start", "stop", "status"])
parser.add_argument(
    "--data-dir",
    default=os.getenv(
        "DATA_DIR",
        str(Path.home() / "Library/Application Support/GuangyaOrganizer")
        if sys.platform == "darwin"
        else str(ROOT / "data/config"),
    ),
)
parser.add_argument("--port", type=int, default=8796)
args = parser.parse_args()
directory = Path(args.data_dir)
directory.mkdir(parents=True, exist_ok=True, mode=0o700)
pid_path = directory / "server.pid"
pid = int(pid_path.read_text()) if pid_path.exists() else None
alive = False
if pid:
    try:
        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        pass
if args.action == "status":
    print(f"running pid={pid} http://127.0.0.1:{args.port}" if alive else "stopped")
elif args.action == "stop":
    if alive:
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
    pid_path.unlink(missing_ok=True)
    print("stop requested")
else:
    if alive:
        print(f"already running pid={pid}")
        sys.exit(0)
    env = dict(os.environ, DATA_DIR=str(directory), HOST="127.0.0.1", PORT=str(args.port))
    fd = os.open(directory / "organizer.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as stream:
        process = subprocess.Popen(
            [sys.executable, "-m", "guangya.app"],
            cwd=ROOT,
            env=env,
            stdout=stream,
            stderr=stream,
            start_new_session=True,
        )
    pid_path.write_text(str(process.pid))
    print(f"started pid={process.pid} http://127.0.0.1:{args.port}")

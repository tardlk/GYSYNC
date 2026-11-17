"""光鸭存储适配所需的进度和临时目录端口。"""

import threading
from pathlib import Path
from types import SimpleNamespace

_stop = threading.Event()
global_vars = SimpleNamespace(is_transfer_stopped=lambda _: _stop.is_set())
settings = SimpleNamespace(TEMP_PATH=Path("/tmp"))


def transfer_process(_path):
    return lambda _progress: None

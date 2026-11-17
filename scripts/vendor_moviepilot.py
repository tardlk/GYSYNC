"""提取固定版本的 MoviePilot 媒体引擎，保留原始代码及逐文件来源校验。"""

import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent / "MoviePilot"
TARGET = ROOT / "guangya/mp_core"
seeds = [
    "app.domain.metainfo",
    "app.domain.context",
    "app.domain.projection.tmdb",
    "app.modules.themoviedb.tmdbapi",
    "app.modules.themoviedb.scraper",
    "app.foundation.dom",
    "app.foundation.size",
    "app.schemas.tmdb",
]
seeds += [
    "app.modules.themoviedb.tmdbv3api."
    + ".".join(path.relative_to(SOURCE / "app/modules/themoviedb/tmdbv3api").with_suffix("").parts)
    for path in (SOURCE / "app/modules/themoviedb/tmdbv3api").rglob("*.py")
    if path.name != "__init__.py"
]
seeds.append("app.modules.themoviedb.tmdbv3api")
seen = {}
pending = seeds[:]
allowed = ("app.domain.", "app.foundation.", "app.schemas.", "app.modules.themoviedb.")
while pending:
    module = pending.pop()
    if module in seen:
        continue
    path = SOURCE / Path(*module.split(".")).with_suffix(".py")
    if not path.exists():
        path = SOURCE / Path(*module.split(".")) / "__init__.py"
    if not path.exists():
        continue
    # 不导入宿主 package 根入口；包初始化在下面生成为空文件。
    if path.name == "__init__.py" and module != "app.modules.themoviedb.tmdbv3api":
        continue
    source = path.read_text()
    seen[module] = source
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and (node.module.startswith(allowed) or node.module in {item.rstrip(".") for item in allowed})
        ):
            pending.append(node.module)
            for alias in node.names:
                pending.append(node.module + "." + alias.name)
# TemplateContextBuilder 是 MP 原始的命名变量构建器，不携带其消息系统。
path = SOURCE / "app/application/messaging/message.py"
tree = ast.parse(path.read_text())
node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TemplateContextBuilder")
source = "\n".join(path.read_text().splitlines()[node.lineno - 1 : node.end_lineno])
header = """from __future__ import annotations
import re
import socket
from typing import Any, Dict, List, Optional, Union, overload
from guangya.mp_core.domain.context import MediaInfo, MusicInfo, TorrentInfo
from guangya.mp_core.domain.meta.metabase import MetaBase
from guangya.mp_core.domain.meta.metamusic import MetaMusic
from guangya.mp_core.schemas.tmdb import TmdbEpisode
from guangya.mp_core.schemas.types import MUSIC_ENTITY_ALBUM
from guangya.mp_core.foundation import size as size_tools
from guangya.mp_core.foundation.text import convert as zhconv_convert
from types import SimpleNamespace
TransferInfo = Any
def get_chain_runtime_config_snapshot():
    return SimpleNamespace(music_metadata_to_simplified=False)
_ALBUM_TRAILING_YEAR_RE = re.compile(r"(?:[\\s\\u3000]*[\\(\\[（【]\\s*(?:19|20)\\d{2}\\s*[\\)\\]）】])+$")
"""
(TARGET / "naming.py").write_text(
    "# Extracted from MoviePilot; GPL-3.0. Runtime imports adapted.\n" + header + "\n" + source + "\n"
)
manifest = []
for module, source in sorted(seen.items()):
    relative = Path(*module.split(".")[1:])
    if module == "app.modules.themoviedb.tmdbv3api":
        relative /= "__init__.py"
    else:
        relative = relative.with_suffix(".py")
    path = TARGET / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    adapted = source.replace("from app.", "from guangya.mp_core.").replace("import app.", "import guangya.mp_core.")
    path.write_text("# Vendored from MoviePilot; GPL-3.0. Imports adapted for standalone execution.\n" + adapted)
    manifest.append({"source": str(relative), "original_sha256": hashlib.sha256(source.encode()).hexdigest()})
for path in list(TARGET.rglob("*.py")):
    for parent in path.parents:
        if parent == ROOT:
            break
        if parent.is_relative_to(ROOT / "guangya"):
            init = parent / "__init__.py"
            if not init.exists():
                init.write_text("")
(ROOT / "LICENSE").write_bytes((SOURCE / "LICENSE").read_bytes())
(ROOT / "UPSTREAM.json").write_text(
    json.dumps(
        {
            "repository": "https://github.com/jxxghp/MoviePilot",
            "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip(),
            "license": "GPL-3.0",
            "files": manifest,
            "extractions": [{"source": "app/application/messaging/message.py", "symbol": "TemplateContextBuilder"}],
        },
        ensure_ascii=False,
        indent=2,
    )
    + "\n"
)
print("Vendored", len(seen), "modules plus TemplateContextBuilder")

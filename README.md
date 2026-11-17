# GYSYNC · 光鸭整理

[![Docker CI](https://github.com/tardlk/GYSYNC/actions/workflows/docker.yml/badge.svg)](https://github.com/tardlk/GYSYNC/actions/workflows/docker.yml)

独立运行的媒体整理服务：光鸭上传、Telegram 分享转存、光鸭盘内整理、本地整理。使用固定版本的 MoviePilot 文件名解析、TMDB 名称匹配、命名变量和 NFO/图片刮削实现，运行时无需安装或连接 MoviePilot。

## 功能

- **分享转存**：私聊 Bot 发送光鸭链接 → 独立任务源目录 → 完整清单核对 → 自动识别整理 → 回复任务结果。
- **光鸭整理**：读取所选云盘目录，先生成预览；确认后在盘内移动/复制、改名、同步字幕、生成 NFO 与图片。
- **本地整理**：挂载源目录和媒体目录，支持移动、复制、硬链接；可选择整理后上传成品及本次生成的刮削文件。
- **光鸭上传**：服务器文件/目录上传、秒传、OSS 分片与断点记录，保留本地源文件，同名目标跳过。
- **Web 管理**：任务状态、源/目标路径预览、操作回执、手动指定 TMDB ID、二维码登录、独立命名模板与 Telegram 设置。

硬链接要求源与目标位于同一文件系统、同一挂载中；需要使用硬链接时，建议额外挂载一个共同的 `/data` 根目录，把源与媒体目录设为其子目录。默认两个独立绑定挂载适合移动或复制。

当前支持电影、电视剧的常规视频文件及匹配的外置字幕。蓝光目录包、多来源音乐整理、MP 插件事件/自定义分类策略不在此版本范围内。ISO 以单文件处理；不会分析光盘内部结构。文件名提供的画质、编码等字段沿用 MP 解析；本版不对视频内容做额外探测。

## Docker 部署

需要 Docker Engine 与 Compose。镜像为 `ghcr.io/tardlk/gysync:latest`，支持 Linux AMD64 / ARM64。源码内不包含任何用户 Key、Bot Token 或登录凭据。

```bash
git clone https://github.com/tardlk/GYSYNC.git
cd GYSYNC
cp .env.example .env
mkdir -p data/config data/downloads data/media
# 在 .env 中填写运行用户的 PUID/PGID，以及实际要挂载的目录。
docker compose -f compose.release.yaml pull
docker compose -f compose.release.yaml up -d
```

访问 <http://127.0.0.1:8796>。首次管理密码保存在挂载配置目录的 `admin-password.txt` 中；`config.json` 和数据库仅容器运行用户可读。目录必须允许 `.env` 中指定的用户写入。默认只监听本机；NAS 上可将 `BIND_HOST` 设为 NAS 地址，结合自己的网络访问控制使用。

容器挂载关系：

| 主机配置 | 容器路径 | 用途 |
|---|---|---|
| `CONFIG_DIR` | `/config` | 私有配置、SQLite、上传断点、管理密码 |
| `DOWNLOADS_DIR` | `/downloads` | 本地源文件 |
| `MEDIA_DIR` | `/media` | 本地整理目标 |

在设置页填写自己的 **TMDB API Key**（[申请说明](https://developer.themoviedb.org/docs/getting-started)）、扫码登录光鸭，并设置云盘源/目标目录。密钥字段留空保存会保留已有值，接口不返回明文。账号数据只在配置卷中，镜像可独立迁移。

仓库提供 `./docker-project` 包装命令，可替代 `docker compose`；本机已安装项目专用 Colima 工具时会自动选用隔离的 Docker 环境，机器重启后可运行 `./docker-project up -d` 恢复。

更新：`docker compose -f compose.release.yaml pull && docker compose -f compose.release.yaml up -d`。停止：`docker compose -f compose.release.yaml down`，配置卷保留。不要同时用多个实例挂载同一配置目录；进程锁会拒绝第二个任务 worker。

若要从源码构建，使用 `docker compose up -d --build`（根目录的 `compose.yaml`），或 `./docker-project up -d --build`。发布镜像部署使用 `compose.release.yaml`；两种方式共用 `.env` 和配置目录，请停掉原容器后切换。

### 自动构建与镜像标签

[GitHub Actions](https://github.com/tardlk/GYSYNC/actions/workflows/docker.yml) 在推送 `main`、`v*` 标签或手动触发时运行。测试通过后，分别在原生 AMD64、ARM64 runner 上构建镜像并启动容器检查健康接口与打包资源，再合并发布到 GHCR。PR 运行相同验证，不发布镜像。流水线使用仓库的 `GITHUB_TOKEN`，无需另填 Docker Hub 密码。

| 标签 | 用途 |
|---|---|
| `latest` | `main` 最近一次通过验证的构建 |
| `sha-<完整提交ID>` | 固定到一次源码提交 |
| `0.1.0`、`0.1` 等 | 推送匹配 `pyproject.toml` 版本的 `v0.1.0` 标签后发布 |

指定版本时在 `.env` 中设置 `IMAGE_TAG`。发布新版本先修改 `pyproject.toml` 并更新锁文件，提交后再创建并推送对应的 `v版本号` 标签。

## Telegram

1. 在 BotFather 创建/找回 Bot，填写 Token。
2. 白名单填写你自己的数字用户 ID，多个 ID 用逗号分隔，然后启用。
3. **同一 Bot 只交给一个程序轮询**。从 MP 迁移时先关闭 MP 的对应 Telegram 通道，再启用这里的 Bot。

命令菜单由本服务注册。只处理白名单用户的私聊。

| 命令 | 行为 |
|---|---|
| 直接发光鸭链接 / `/guangya 链接 提取码：1234` | 转存后自动整理 |
| `/tasks` | 最近任务与状态 |
| `/organize_local` | 预览默认本地源目录 |
| `/organize_cloud` | 预览默认光鸭源目录 |
| `/confirm 任务ID` | 执行有效的整理预览 |
| `/retry 任务ID movie或tv TMDB数字ID` | 指定媒体并重新生成预览 |
| `/upload "本地绝对路径"` | 上传容器内的文件/目录 |
| `/help` | 使用帮助 |

整理、上传不由聊天消息指定任意本机权限：本地路径限定在设置的挂载根目录。文件浏览也是服务器/容器的路径，不是浏览器所在电脑的路径。

## 数据与失败处理

- 分享按分享 ID 去重。同一资源重复发送会返回已有任务，避免重复占空间。仅在转存写入前失败的任务允许重发链接更正提取码；结果不明的转存不会重放。提取码不在前端任务结果中显示。
- 单 worker 串行处理，最多 100 个待处理任务。转存和文件写入前先保存阶段及逐文件回执。
- 转存后完整核对分享清单的路径、条目类型和文件大小，核对成功才进入整理。
- 手动任务默认只读预览；执行时再次核对源文件身份、大小和修改时间。源/目标不可重叠，目标同名不覆盖。
- 网络超时、崩溃和运行中断的写入结果不明时标记 `needs_review`，不会自动重放转存、上传或移动。
- 识别失败、年份型误识别会保留资源。可指定 TMDB ID 重新预览；存在未确认写入时，先核对存储内容，再从媒体整理页发起新的预览。
- `partial` 表示文件整理已完成，但部分刮削或后续上传未完成。先查看操作记录；重新移动同一文件不会修复已完成媒体的元数据问题。
- 自动重试仅针对 Telegram 通知（有限次数）及上游客户端明确允许的只读/认证重试。通知失败不阻塞文件处理，`/tasks` 和 Web 保留真实结果。
- 本地移动在同文件系统中采用不覆盖的硬链接再移除源名；跨文件系统先排他创建目标、复制并确认大小，再移除源文件。需要完全保留源文件时选择复制或硬链接。
- 移动后的空目录、未参与整理的说明文件保留。默认至少 0.5 秒云盘请求间隔，不能据此保证不触发上游风控。

备份整个配置目录，且应在服务停止时复制 SQLite 数据库或使用 SQLite 自带备份功能。配置及数据库含令牌/分享凭据，不应公开上传。NFO/图片采用存在则保留策略；本版本不自动替换媒体库中的旧元数据。

## 从本机 MoviePilot 迁移

先停止独立服务，然后执行：

```bash
uv sync --frozen
.venv/bin/python scripts/migrate_moviepilot.py \
  --source-config /原MoviePilot配置目录 \
  --target-config /独立服务配置目录
```

脚本只读 MP 的 SQLite 和分享队列，复制光鸭登录与 Telegram 凭据；不读取 MP 内置 TMDB Key，需配置自己的 Key。既有分享只迁移去重和源路径，标记待核对，不恢复旧程序中未确认的写操作。迁移后的 Telegram 保持关闭，待旧服务停用后再在这里开启。

Docker 中请把本地源/目标和允许根目录设为容器路径 `/downloads`、`/media`。历史任务保留旧路径作为审计记录，跨环境迁移后请使用当前挂载路径创建新预览。

## 本机开发

Python 3.14+。运行依赖由 `uv.lock` 固定，镜像通过 `requirements.lock` 验证依赖源码/轮子的哈希。

```bash
uv sync --frozen
./organizer start
./organizer status
./organizer stop
uv run --frozen pytest -q
uv run --frozen ruff check guangya scripts tests
```

macOS 默认数据目录是 `~/Library/Application Support/GuangyaOrganizer`。其他系统默认是 `./data/config`。也可直接通过 `DATA_DIR=/config python -m guangya.app` 前台运行；默认端口 8796，只启动一个 worker。

### MoviePilot 代码来源

- 原项目：<https://github.com/jxxghp/MoviePilot>
- 固定提交与逐文件原始 SHA-256：`UPSTREAM.json`。
- `guangya/mp_core/`：提取的文件名解析、领域模型、TMDB 匹配、刮削与命名变量代码。`runtime/`、`adapters/network/http.py` 是独立适配端口。
- `scripts/vendor_moviepilot.py /MP源码目录` 可重新提取；升级后需要重新核对依赖、模板和回归样例，不自动追踪上游。
- `guangya/cloud_client.py`、`cloud_api.py`、`share_protocol.py` 从 [光鸭同步插件](https://github.com/tardlk/MoviePilot-Plugins) 迁移并替换宿主依赖，保留 GPLv3 来源。

本项目以 GPLv3 发布，完整许可证见 `LICENSE`。使用 TMDB API，但未经 TMDB 认可或认证；元数据及图片版权归相应权利人。API 使用应遵守 [TMDB 条款](https://www.themoviedb.org/api-terms-of-use)。

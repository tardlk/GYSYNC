# 验证记录

验证日期：2026-11-16（Asia/Shanghai）。本机 Apple Silicon，Docker Linux ARM64。

- `.venv/bin/pytest -q`：49 项通过。测试全部隔离真实账号和网络；含源文件在复制途中变化的保留验证、写入中断不重放、白名单/私聊、私有登录、密钥脱敏、通知 outbox、菜单同步故障不阻塞收消息。
- `.venv/bin/ruff check guangya scripts tests`：通过。
- `uv lock --check`、Python 编译、Node `--check guangya/web/app.js`、`docker compose config -q`：通过。
- 官方 Python 基础镜像初次经 Docker 下载时遇到 TLS/EOF；通过官方 registry 获取并逐层验证 SHA-256 后加载本机缓存。最终已实际构建 Docker 镜像。
- Docker 内：首次默认配置启动、健康检查、管理登录、无 MP 宿主导入均通过。
- 真实 TMDB Key：连接、电影识别、剧集解析、MP 命名、NFO XML、海报地址生成通过。
- 本地真实文件联调：只读预览、实际复制、字幕改名、NFO/图片写入通过。使用可明确辨认的非播放用测试文件，未借此测试视频内容探测。
- 真实光鸭账号：隔离联调目录中的上传、盘内移动与改名、字幕、NFO/图片上传通过；源/目标身份与大小均已核对。
- 迁移的既有分享再次提交只命中原任务，没有重复转存约 82 GiB 的用户资源。
- 容器重建后：私有配置、登录会话、五条任务记录、任务线程及 Bot 线程均保留正常；当前容器源文件哈希与交付源码一致。
- Bot 已由 MP 切换到独立服务，Telegram 服务器端菜单确认为本服务的 8 条命令。2026-11-17，用户确认发送 `/tasks` 后已收到新服务的最近任务与联调记录，Telegram 实际收发验收通过。
- 旧本机 MP 前后端和其 Bot 通道已停止，原配置保留于私有数据目录，测试容器已移除。

镜像 ID：`sha256:ca7f03b3b260ca4065a77a14c93293cc86b08398c61f0463a24cdfc1f4c86701`

镜像大小：206.0 MiB。

一次空闲容器采样：`79.5MiB / 1.914GiB 0.23%`，不包含 macOS 上 Docker 虚拟机本身的资源开销。

尚未实测：大文件长时间上传、不同 NAS 文件系统/跨卷权限组合、完整浏览器视觉验收。

单元测试当前有一条 Starlette TestClient 对 httpx 的弃用提示，不影响上述测试通过；运行服务不使用 TestClient。

## GitHub Actions 发布验收

2026-11-17，源码已发布至 [tardlk/GYSYNC](https://github.com/tardlk/GYSYNC)，初始提交 `35e1ccb0861e59791be49b7f6f39e1145ddfb51c`。

- [首次流水线](https://github.com/tardlk/GYSYNC/actions/runs/44051521307)：全部成功。Linux AMD64 上 49 项测试、Ruff、前端语法和两份 Compose 配置检查通过。
- Linux AMD64、ARM64 分别在原生 runner 上构建；两个容器的 `/health`、前端静态文件、上游来源清单、GPL 许可证打包检查均通过。
- 多平台镜像已公开发布为 `ghcr.io/tardlk/gysync:latest`，同时提供完整提交 ID 的 `sha-` 标签。未配置或上传用户账号凭据；CI 使用仓库自身的 `GITHUB_TOKEN` 发布。
- 匿名访问 GHCR 成功；下载并校验 OCI index 的 SHA-256，确认包含 `linux/amd64` 和 `linux/arm64`。另有 provenance 证明清单，其平台显示为 `unknown/unknown`，不是第三种运行平台。
- 此次镜像清单摘要：`sha256:35894282a0ac0c57751508764ecee17e31158e8d3caecf7ad74cf4ec9fa648c8`。`latest` 会随以后通过验证的 main 构建更新，需固定版本时使用提交标签或摘要。
- 本地补做发布前镜像构建、全新配置容器健康检查和打包文件检查，均通过；生产实例及其配置卷继续正常运行。

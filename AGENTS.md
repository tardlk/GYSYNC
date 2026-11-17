# 光鸭整理交接

独立 FastAPI + SQLite 服务，Python 3.14；根目录 `compose.yaml` / `Dockerfile`，页面 `guangya/web/`，端口 8796。

- `service.py` 拥有任务生命周期和副作用。单 worker 文件锁；Telegram 只由同一主进程启动。禁止在 API 导入时启动线程或访问云盘。
- `db.py`：SQLite WAL，短事务；网络操作不能持有数据库事务。写入阶段先持久化，重启不重放结果不明的任务。通知使用持久 outbox，不阻塞文件操作。
- `storage.py`：本地路径限制在明确挂载根目录，拒绝符号链接；同名目标不覆盖。预览没有 mkdir/move/upload 副作用，执行前重查文件身份。
- `media.py` 调用 `mp_core` 原实现；提取版本和原始 SHA 在 `UPSTREAM.json`。独立运行禁止从活跃 MP checkout 或 `app.*` 导入。枚举写入数据库前将 TMDB media_type 标准化为 movie/tv，避免持久化后把电影误当电视剧。
- `cloud_client.py` / `cloud_api.py` / `share_protocol.py` 为现有插件移植；更改严格查询/写入确认时需补充故障回执测试，不放宽到“请求已接收就是完成”。
- `config.py`：私有文件 0600；不把 Key、Bot Token、盘登录态写入代码、日志、README、fixtures 或镜像。密钥字段只写，不通过 API 明文返回。
- `telegram.py`：只接受数字白名单私聊。令牌不出现在日志中；不得启动第二套轮询抢占同一 Bot。迁移时保留旧服务配置，确认新服务可用后切换。
- 开发检查：`.venv/bin/pytest -q`、`.venv/bin/ruff check guangya scripts tests`、Node `--check guangya/web/app.js`、`docker compose config -q`、镜像构建及容器健康检查。
- 单元测试只用临时目录与回放接口。真实云盘联调只用用户授权资源或明确的隔离测试目录，保留操作记录；失败后先核对，禁止自动重新转存。
- 仅此新项目在当前任务范围内。已有 MP 和插件仓库的未提交工作不应清理或覆盖。更改此项目无需修改宿主 MP。

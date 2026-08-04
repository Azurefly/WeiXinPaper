# 数据库迁移与回滚

当前 SQLite schema 版本为 `213`，以 `backend/db.py` 中的 `SCHEMA_VERSION` 为唯一事实来源。

## 启动迁移

1. 检查原数据库 `PRAGMA integrity_check`。
2. 使用 SQLite Backup API 创建一致性备份。
3. 逐版执行迁移，更新 `schema_meta`。
4. 对结果再次执行完整性检查。
5. 任一步失败时从迁移前备份恢复。

## 备份

停止服务后，成对备份：

- `studio.db`
- `.master.key`
- 如仍存在则包含 `studio.db-wal` 与 `studio.db-shm`

更推荐使用 SQLite Backup API 或产品「设置 → 数据管理」导出功能。不要单独复制 WAL 文件。

## 回滚

1. 停止应用。
2. 保留当前失败现场副本用于排查。
3. 同时恢复数据库和与之匹配的主密钥。
4. 启动前运行完整性检查，启动后验证登录、凭证解密、文章版本和发布回执。

Windows 旧 32 字节主密钥首次读取时会升级为当前用户 DPAPI 封装，底层加密密钥不变，因此已有密文仍可解密。

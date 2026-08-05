# 架构

## 运行形态

项目是无前端构建步骤的单机 Web 应用：

- `web/`：原生 JavaScript 单页界面与 CSS。
- `backend/server.py`：HTTP API、静态文件、认证、CSRF 和发布门禁。
- `backend/workflow.py`：来源准备、框架、正文、审校和封面任务。
- `backend/db.py`：SQLite schema 213、迁移、版本、任务和回执。
- `backend/ai_engine.py` / `wechat_api.py`：外部 AI 与微信 API 适配。
- `desktop.py`：可选 PyWebView 桌面容器，复用同一后端与页面。

## 一致性模型

- 每篇文章以 `revision` 作为乐观锁，同文章的前端保存串行化。
- 预览、人工终审和发布均绑定 revision 与内容指纹。
- 发布前冻结 HTML 快照；发布期间的新编辑不会被旧回执覆盖。
- 来源快照由来源 URL 与内容哈希定位，刷新来源会使下游审校和发布状态失效。

## 工作流状态

```text
queued → running → succeeded
                 ├→ blocked
                 ├→ failed
                 ├→ timeout
                 └→ cancelled
```

同一文章只允许一个活跃任务。失败后可按「仅审校、保留正文、从框架重做、全部重做」选择重试边界。

## 主要 API 边界

- `/api/v2/auth/setup`：仅无用户或旧版未领取 admin 可用的一次性初始化。
- `/api/v2/auth/*`：登录、改密、会话与退出。
- `/api/v2/workflows`：唯一新建内容入口。
- `/api/v2/projects/*`：编辑、版本、预览、终审和发布。
- `/api/v2/settings/*`：通用策略、AI 与微信凭证。
- `/api/v2/data/*`：无密钥备份导出与恢复。
- `/api/v2/logs`：已脱敏的运行日志查询。

## 非目标

当前不是多租户 CMS、实时多人协作系统或全网原创性鉴定平台。界面不应展示未接入后端的伪功能。

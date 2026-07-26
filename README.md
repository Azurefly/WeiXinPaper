# 公众号 AI Studio 2.1.3 持续整改候选版

公众号 AI Studio 是本地运行的微信公众号 AI 内容工作台。2.1.3 针对 2.1.1 综合审计中的远程启动、AI 密钥出站、自动保存并发、发布快照、工作流重试、数据库迁移和交互闭环问题进行了整改。

> 当前定级：**Release Candidate**。本地功能、安全与一致性自动化通过；真实 AI completion、微信公众号 `draft/add`、Windows DPAPI 实机和真实服务浏览器 E2E 仍需在具备凭证及对应环境的机器上完成，未标记为 Final。

## 核心流程

```text
输入来源或创作目标
→ 来源/证据准备
→ 严格事实门禁
→ 文章框架
→ 正文写作
→ 自动审校或明确跳过
→ 等待所有保存完成
→ 生成当前 revision 发布快照
→ 人工终审当前 revision
→ 同步不可变快照
→ 按 revision 回写微信回执
```


## 2.1.3 继续整改

- 文章中心改为服务端分页、搜索和总数统计，默认每页 50 篇，可用同一界面管理万级文章库。
- 新增 `verify_capacity.py`，使用真实 SQLite 与真实 HTTP API 验证 1 万篇文章、随机分页、检索、连续 100 次保存、版本上限和完整性。
- 修复真实 AI 外部验证脚本的正文调用参数错误。
- 微信外部验证支持从本地 PNG/JPEG/GIF/WEBP 上传永久封面素材，再执行真实 `draft/add`；可选验证后清理。
- 新增 `verify_browser_service.py/.cmd`，在 Chrome、Edge 或 Chromium 上执行不依赖伪 AI 的真实服务 UI 验收。
- 新增 `verify_windows_dpapi.py/.cmd`，在 Windows 普通用户会话中验证旧原始主密钥升级、重启解密、新密钥保护和密文篡改拒绝。

## 2.1.3 主要整改

- 内置 HTTP 服务只允许绑定 `127.0.0.1`、`::1` 或 `localhost`，远程访问必须通过同机 HTTPS 反向代理。
- AI 验证和生成固定到已验证公网 IP，校验实际对端，禁止重定向携带 `Authorization`。
- 同文章保存请求严格串行；关闭或刷新页面时存在未保存内容会提示。
- 标题、摘要、正文和封面发生 revision 冲突时，四类本地字段都保留。
- 发布前冻结 revision、正文指纹和 HTML 哈希；发布期间正文变化时，微信回执记为旧版本同步，不覆盖当前文章状态。
- 同文章只允许一个活跃工作流；重试必须选择“仅审校、保留正文、从框架重做、全部重做”。
- 严格事实模式在无可核验证据时直接暂停。
- 来源快照按“来源 URL + 内容哈希”建模，不再覆盖共享快照。
- SQLite 迁移使用 backup API、迁移前后 `integrity_check`、迁移日志和失败回滚。
- Windows 默认用当前用户 DPAPI 保护主密钥；Linux/macOS 使用仅当前用户可读的密钥文件，也可配置主密码。
- 版本历史、恢复、回收站、永久删除和任务筛选进入用户界面。
- 公众号预览和提交 HTML 使用同一服务端渲染结果。
- 正式运行包不包含测试适配器启用标记，仅设置环境变量不能获得伪造 AI/微信成功。

## 运行要求

- Python 3.11 或更高版本
- 核心运行不需要 Node.js、npm 或第三方 Python 包
- 仅真实浏览器验收脚本需要可用的 Chrome/Edge/Chromium；使用 Playwright 驱动时需在验收机安装 Playwright
- 数据默认保存在 `data/studio.db`

### Windows

```text
setup_windows.cmd
start_windows.cmd
```

访问：`http://127.0.0.1:5000/`

### Linux / macOS

```bash
./setup_unix.sh
./start_unix.sh
```

## 远程访问

不要让内置服务监听 `0.0.0.0`。推荐在同一台机器上配置 Nginx、Caddy 或 IIS：

1. 内置服务继续监听 `127.0.0.1:5000`。
2. 反向代理对外提供受信任的 HTTPS。
3. 设置固定公开 Origin 和后端 Basic Auth：

```bash
export STUDIO_PUBLIC_ORIGIN='https://studio.example.com'
export STUDIO_AUTH_USER='studio-admin'
export STUDIO_AUTH_PASSWORD='使用高强度随机密码'
python3 start.py
```

反向代理必须保留公开 `Host`，并将 `Authorization` 传给回环后端。Basic Auth 只允许经 HTTPS 到达浏览器端。

## 密钥与备份

- Windows：默认使用当前用户 DPAPI 保护 `data/.master.key`。
- Linux/macOS：`data/.master.key` 权限必须为 `0600`。
- 可通过 `STUDIO_MASTER_PASSWORD` 使用不少于 12 位的主密码派生密钥。
- 数据迁移、搬迁和灾备时，数据库与主密钥必须成对备份。
- 源码包和运行包均不包含任何生成后的 `.master.key`、数据库或真实凭证。

## 测试

源码包核心自动化：

```bash
python test_all.py
```

运行包自检：

```bash
python install.py
python test_runtime.py
```

浏览器真实服务 E2E：

```bash
RUN_BROWSER_E2E=1 python test_all.py
```

当前交付环境的 Chromium 被宿主策略禁止访问本地 HTTP，因此该项未记为通过。界面截图由 `tests/generate_screenshots.py` 使用最终 `web/app.js` 和 `web/styles.css` 渲染，但不替代真实服务 E2E。

## 真实外部链路验证

验证脚本会调用真实 AI 框架、正文、审校链路；微信公众号验证会真实创建一篇草稿：

```bash
export STUDIO_VERIFY_AI_KEY='...'
export STUDIO_VERIFY_AI_MODEL='...'
export STUDIO_VERIFY_WECHAT_APPID='...'
export STUDIO_VERIFY_WECHAT_SECRET='...'
# 二选一：复用已有永久素材，或上传本地封面文件
export STUDIO_VERIFY_WECHAT_THUMB_MEDIA_ID='...'
# export STUDIO_VERIFY_WECHAT_COVER_FILE='/absolute/path/cover.png'
# export STUDIO_VERIFY_WECHAT_CLEANUP=1  # 验证后删除本次上传的封面素材
export STUDIO_VERIFY_EXTERNAL_FULL=1
python verify_external_links.py

# 万级容量与连续编辑验证（真实 SQLite + HTTP API）
python verify_capacity.py

# 目标机器的一小时浸泡验收仍应结合真实编辑器操作、浏览器与系统监控执行

# Windows DPAPI 实机验证
verify_windows_dpapi.cmd

# Chrome/Edge 真实服务浏览器验证
verify_browser_service.cmd
```

脚本不会输出 API Key、AppSecret 或 access token。没有设置 `STUDIO_VERIFY_EXTERNAL_FULL=1` 时，不会创建微信草稿。

## 文档

- `docs/2.1.3_综合整改与验证报告.md`
- `docs/2.1.3_发布门禁状态.md`
- `docs/2.1.3_数据库迁移与回滚.md`
- `docs/2.1.3_外部链路验证结果.json`

# 验证指南

## 默认门禁

```bash
python3 test_all.py
```

执行顺序：JavaScript 语法检查、Python 编译检查、`tests/` 全量 unittest 发现。测试数量以命令的实时发现结果为准，覆盖 API、保存冲突、发布快照、迁移、安全出站、日志脱敏、数据导入导出和桌面包。

## 真实服务浏览器 E2E

```bash
python3 -m pip install playwright
python3 -m playwright install chromium
RUN_BROWSER_E2E=1 python3 test_all.py
```

E2E 使用临时数据库，完整覆盖首次登录、强制改密、创作、串行自动保存、预览、终审、版本、任务诊断、设置信息架构与移动端。截图默认写入系统临时目录，不污染仓库。

## 容量与平台

```bash
python3 verify_capacity.py
python3 verify_browser_service.py
```

Windows 还需执行：

```text
verify_windows_dpapi.cmd
verify_browser_service.cmd
```

## 凭证化外部链路

```bash
export STUDIO_VERIFY_AI_KEY='...'
export STUDIO_VERIFY_AI_MODEL='...'
export STUDIO_VERIFY_WECHAT_APPID='...'
export STUDIO_VERIFY_WECHAT_SECRET='...'
export STUDIO_VERIFY_WECHAT_THUMB_MEDIA_ID='...'
export STUDIO_VERIFY_EXTERNAL_FULL=1
python3 verify_external_links.py
```

可用 `STUDIO_VERIFY_WECHAT_COVER_FILE` 上传真实封面，用 `STUDIO_VERIFY_WECHAT_CLEANUP=1` 在验证后清理该素材。该流程会真实调用 AI 和微信 `draft/add`，必须由凭证持有者授权后执行。

## 结果记录规则

发布记录只保留当次时间、环境、命令、通过数与未通过项。不把历史审计 HTML、旧截图或带过时结论的 JSON 继续堆在源码仓库。

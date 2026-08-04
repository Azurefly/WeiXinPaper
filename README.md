# 公众号 AI Studio 2.1.3

一个本地优先的微信公众号内容工作台：从网页来源或创作主题出发，完成 AI 写作、审校、人工终审和微信草稿同步。

## 产品边界

- 「强制引用模式」要求可核验的网页来源与 `[来源N]` 标记；它不等同于第三方事实核查。
- 「来源文本重合」只比较当前抓取的来源；它不会声称全网原创度。
- 发布操作创建公众号草稿，不会直接群发。发布页会明确显示目标账号、AppID 后缀和 revision。

## 快速开始

要求 Python 3.11+，核心运行无第三方 Python 依赖。

```bash
./setup_unix.sh
./start_unix.sh
```

Windows：

```text
setup_windows.cmd
start_windows.cmd
```

然后访问 `http://127.0.0.1:5000/`。首次启动会在数据目录生成 `.initial_password`，登录后必须立即改密。

## 工作流

```text
来源/主题 → 证据门禁 → 框架 → 正文 → 自动审校
          → 保存 revision → 服务端预览 → 人工终审
          → 冻结发布快照 → 公众号草稿 → revision 回执
```

## 配置与数据

- AI 与公众号凭证在「设置」中验证后保存。
- 数据默认位于 `data/studio.db`，主密钥位于同目录的 `.master.key`；两者必须成对备份。
- 数据导出/导入位于「设置 → 数据管理」，导出文件不包含密码、会话和 API 密钥。

## 验证

```bash
python3 test_all.py
```

运行包自检：

```bash
python3 install.py
python3 test_runtime.py
```

需要 Playwright 与 Chromium 的真实服务 E2E：

```bash
RUN_BROWSER_E2E=1 python3 test_all.py
```

凭证化的真实 AI/微信链路和 Windows DPAPI 必须在对应环境验收，详见 [验证指南](docs/VERIFICATION.md) 与 [发布检查清单](docs/RELEASE_CHECKLIST.md)。

## 远程访问

内置服务只允许监听回环地址。对外访问必须通过同机 HTTPS 反向代理，并设置：

```bash
export STUDIO_PUBLIC_ORIGIN='https://studio.example.com'
export STUDIO_AUTH_USER='studio-admin'
export STUDIO_AUTH_PASSWORD='使用高强度随机密码'
python3 start.py
```

不得将内置服务直接绑定到 `0.0.0.0`。安全约束见 [安全说明](docs/SECURITY.md)。

## 文档

- [架构](docs/ARCHITECTURE.md)
- [安全](docs/SECURITY.md)
- [验证](docs/VERIFICATION.md)
- [迁移与回滚](docs/MIGRATION.md)
- [发布检查清单](docs/RELEASE_CHECKLIST.md)

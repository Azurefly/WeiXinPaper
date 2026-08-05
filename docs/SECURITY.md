# 安全说明

## 已实施的控制

- 内置服务拒绝非回环监听；远程访问必须经 HTTPS 反向代理。
- 会话 Cookie 使用 `HttpOnly` 和 `SameSite=Strict`；配置 HTTPS 公开 Origin 时自动加 `Secure`。
- 写 API 使用 Origin 检查与双重提交 CSRF token。SSE 只使用同源会话 Cookie，不把 token 写入 URL。
- AI 与来源网络请求先解析公网 IP，固定连接到已验证对端，并拒绝私网、重绑定和携凭据重定向。
- AI Key 与微信 AppSecret 加密存储；Windows 使用当前用户 DPAPI，Linux/macOS 的主密钥文件限制为 `0600`。
- 预览和微信提交共用服务端 HTML 消毒器。封面上传验证 MIME、文件签名、解码结果和大小。
- 访问日志只记录路径、状态、耗时和字节数，不记录请求/响应正文。标准输出、文件日志、内存日志和异常栈统一脱敏。
- 500 响应不向客户端暴露 Python `repr`、文件路径或 SQL 细节。
- 管理员只能在无用户或旧版随机密码 admin 从未登录时初始化；完成后初始化接口永久返回冲突，不提供公开注册。

## 部署要求

1. 不要把内置服务监听到 `0.0.0.0`。
2. 远程模式必须设置 `STUDIO_PUBLIC_ORIGIN`、强随机 `STUDIO_AUTH_PASSWORD` 和受信任 HTTPS 证书。
3. 反向代理需保留公开 `Host`，并将 Basic Auth `Authorization` 头传给回环后端。
4. 不要提交 `data/`、`.master.key`、旧版可能遗留的 `.initial_password`、数据库、日志或备份导出文件。
5. 定期更换 AI/微信凭证，并复核 `data/studio.log` 的访问权限。

## 安全边界

内置内容检查是发布前辅助，不代替法务、版权、广告法或微信平台规则审查。

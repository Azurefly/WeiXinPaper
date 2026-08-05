# 发布检查清单

## 必须通过

- [ ] `python3 test_all.py`
- [ ] `RUN_BROWSER_E2E=1 python3 test_all.py`
- [ ] `python3 verify_capacity.py`
- [ ] 最终源码无真实密钥、数据库、日志、初始密码或备份导出
- [ ] `RELEASE_MANIFEST.json` 与 `RELEASE_FILES_SHA256.txt` 由最终文件重新生成
- [ ] 新建数据库和从上一 schema 升级均通过完整性检查
- [ ] 首次管理员初始化、旧版未领取 admin 恢复、登录、改密、退出、会话过期和 CSRF 回归通过
- [ ] 使用真实 AI 凭证完成 plan/draft/review
- [ ] 使用真实微信测试号创建草稿，核对目标账号、revision 与回执
- [ ] Windows 目标机完成 DPAPI、安装、重启和卸载验收
- [ ] macOS 应用签名与启动验收通过

## 发布后

- [ ] 默认只监听回环地址
- [ ] HTTPS 部署的会话与 CSRF Cookie 均含 `Secure`
- [ ] 日志中无密码、API Key、AppSecret、access token、会话、请求正文或响应正文
- [ ] 备份恢复演练通过，数据库与主密钥成对恢复

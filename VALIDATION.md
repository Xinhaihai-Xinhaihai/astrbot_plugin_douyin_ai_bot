# v1.4.1 登录改造验证记录

验证日期：2026-11-10。验证范围：自动二维码登录、Docker/noVNC 人工登录、CDP 复用和登录态判定。

## 已验证

- 完整测试套件 40 项通过。使用本地 Chromium 和构造页面验证 iframe 分段拖动、四段各停 1.5 秒、全程按住、验证框消失后至少 5 秒才截码、二维码占位拒绝、即时发图、刷新去重、超时/取消和 AstrBot 图片组件。
- 人工登录流程通过模拟 CDP 浏览器测试：复用已有 context、不启动本地 Xvfb、每 2 秒检查登录状态、成功后保存登录态、超时后保留登录页；插件断开 CDP 时不会关闭 sidecar Chromium。
- 登录状态判定测试覆盖：只有 `sessionid` 不再视为已登录；需要抖音页面确认，`dy状态` 能区分页面确认、仅有 Cookie 和未登录。
- Python 编译、`_conf_schema.json` 解析、本次修改文件的 Ruff 格式与静态检查、`git diff --check` 均通过。
- `docker-compose.browser.yml`、`.env.browser.example`、Dockerfile 和入口脚本已做静态检查，服务设计为 Chromium CDP 只在 AstrBot 的 Docker 网络内可达，noVNC 默认只绑定 `127.0.0.1`。

## 尚未验证的环境

- 当前 Windows 开发环境没有 Docker，未实际启动 `astrbot-douyin-browser:1.4.1` 镜像，也未在目标 Linux 服务器上完成 noVNC 容器到 AstrBot 的真实 CDP 集成测试。
- 未接入目标 AstrBot 服务器或真实消息平台，未完成真实账号扫码、短信、滑块、消息投递的端到端验收。
- noVNC 人工接管能处理页面上的真实验证，但无法绕过抖音针对账号、服务器 IP、地区或网络的风控。特定服务器仍可能持续拒绝登录。

部署和验收步骤见 README 的「服务器登录」章节。

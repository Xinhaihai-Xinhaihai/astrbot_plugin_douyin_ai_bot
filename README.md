# astrbot_plugin_douyin_ai_bot

抖音 AI 陪看插件（AstrBot v4）

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

- **直播订阅检测**：订阅主播直播间，定时轮询开播状态，开播自动推「封面图 + 直播间链接 + 配文」到指定群，群里说触发词即时查
- **陪看评论**：发个抖音视频链接 → 下载抽帧喂多模态视觉模型「看懂」→ 用登录身份发一条像真人的短评 → 截图推转发群（私聊只回一行文字确认，不再刷屏）
- **主动刷视频**：定时刷一批视频（特别关心 up 优先）→ 逐个打分 → 过线才评论 → 截图推直播转发群
- **互动提醒**：轮询私信新消息、我发的评论被谁回复了，推直播转发群；支持一键回私信
- **服务器扫码登录**：持续截图等待二维码 / 自动尝试拼图滑块 / 验证通过后等 5 秒截码推送；也可导入浏览器 Cookie
- **服务器人工登录**：Docker 部署可连接独立 Chromium，通过 noVNC 在真实浏览器完成扫码、短信或滑块验证；适合自动验证被服务器风控拒绝的场景

不点赞、不收藏、不刷量。评论频率受「同视频冷却 + 全局间隔」双重限制。

## 直播检测原理

- 19 位房间 id（手机分享链接解析出来的）→ 直接 HTTP 拉 reflow 分享页判断，无需浏览器，秒出结果
- 12 位左右的直播间号（live.douyin.com 地址里的）→ Playwright 无头浏览器渲染页面判断
  - 开播：页面含「在线观众 / 本场点赞」或标题含「xxx的抖音直播间」
  - 未开播：页面含「未开播 / 暂未开播 / 直播回放」

抖音 web 接口（webcast enter）带签名校验，直接 HTTP 调用会被拒，因此直播间号走页面渲染最稳。
默认使用系统 Edge（`msedge`），无需额外下载浏览器；也可留空用 Playwright Chromium（需先 `playwright install chromium`）。

### 订阅对象怎么填

1. **最省事（推荐）**：手机抖音 App 进主播直播间 → 右上角「…」→ 分享 → 复制链接，把整段分享文本直接粘给机器人订阅，插件自动展开短链拿房间号。
2. **直播间号**：电脑浏览器打开正在播的直播间，地址栏 `https://live.douyin.com/123456789012/` 里的数字。
3. **抖音号**：`订阅 Lianmin726` 这样直接填也行，自动用无头浏览器定位直播间，并把这个抖音号当昵称做开播校验。

## 陪看评论原理

1. 解析视频链接里的 aweme_id（支持 v.douyin.com 短链和完整链接）
2. HTTP 拿视频信息 + 下载地址，ffmpeg 按 `frame_count` 均匀抽帧
3. 截图 + 标题 + 热评喂视觉模型（配 `comment_style` 提示词）生成 1-2 句口语短评
4. 带登录 cookie 用 UI 自动化发出评论（需要 a_bogus 签名，所以走页面而不是自己造接口）
5. 把「标题 + 作者 + 评论 + Cinnamoroll 相框」渲染成截图，推给触发者 / 截图推送群（1.3.6 起不渲染封面图）

限制：同一视频 `cooldown_hours` 内只评一次；任意两次评论间隔 ≥ `global_cooldown_minutes`。

## 主动刷视频原理

1. 每 `proactive_interval` 分钟跑一轮，每轮最多看 `proactive_batch` 个
2. 候选来源优先级：**特别关心 up 的最新作品** → 已订阅 up 的作品 → 登录态推荐流
3. 去重：看过 / 评过的视频（seen_videos.json，保留 30 天）不再看
4. 逐个 `fetch_video` 后调视觉模型打 **0-10 分**（内容质量 + 口味关键词匹配 + 热评质量，特别关心 up 保底 +1）
5. 分数 ≥ `score_threshold` 才发评论（受全局冷却限制），并渲染截图
6. 过线视频截图推**直播转发群**（`forward_groups`，和开播推送同一个群）；没配转发群才退回 `notify_groups`
7. **本轮打分汇总**：一轮刷完如果没有任何视频过线，推一张打分汇总卡片（和过线截图同一个相框），列出每个视频的标题 / 作者 / 分数 / 打分理由 / 链接，方便知道模型看了啥、为什么没过线；有视频过线就不推汇总（过线的各自单推截图）

## 互动提醒原理

每 `interact_interval` 秒轮询一次（需已登录）：

- **私信**：打开消息中心抓会话列表，新私信推给主人（每会话 30 分钟最多提醒一次防骚扰；首次运行只记水位不补历史）
- **评论被回复**：查最近评过的视频评论区，有人 `@我的昵称` 回复时推给主人
- 主人可以用 `dy互动 私信回复 <内容>` 让 bot 用抖音身份回最近那个会话

## 命令

| 命令 | 说明 |
| --- | --- |
| `订阅 <抖音号/直播间号/分享文本>` | 订阅主播 |
| `取消订阅 <抖音号>` / `订阅列表` | 取消 / 查看 |
| `直播转发 <群号>` / `取消转发 <群号>` | 开播推送群管理 |
| `dy评论 <视频链接>` | 看视频→发短评→截图推转发群（私聊只回文字确认） |
| `dy主动 开/关/刷` | 开/关主动刷；`刷`=立即跑一轮 |
| `dy口味 <词1,词2>` | 设置打分口味关键词 |
| `dy特别关心 <抖音号>` / `dy特别关心 删 <抖音号>` | 管理特别关心的 up（主动刷优先看 ta，打分 +1） |
| `dy主人 <QQ号>` | 设主人 QQ 号（备用标识，推送统一走转发群） |
| `dy互动 开/关` | 开/关私信+评论回复轮询 |
| `dy互动 私信回复 <内容>` | 用抖音身份回最近私信会话 |
| `dy刷状态` | 主动刷/互动/口味/特别关心/主人 一览 |
| `dy登录` | 管理员登录；配置 `browser_cdp_url` 时自动进入 noVNC 人工登录，否则与 `dy_qr` 相同 |
| `dy_remote_login` | 管理员通过 noVNC 在服务器 Chromium 中完成扫码、短信或滑块验证；别名 `dy服务器登录` / `dy人工登录` / `dy远程登录` |
| `dy_qr` | 管理员扫码登录：自动尝试拼图滑块，通过后等 5 秒截码并发回当前会话 |
| `dy_cookie <整段Cookie 或发txt文件>` | 浏览器登录抖音后 F12-Cookie 全选复制，贴过来导入 |
| `dy状态` | 登录态 / 订阅 / 转发群 |
| `添加触发词 <词>` / `删除触发词 <词>` / `触发词列表` | 触发词管理 |
| 触发词（默认：看看腿） | 即时检查直播，有在播的发封面+链接+配文 |

## 服务器登录

服务器提供两条登录路径：

- **推荐：noVNC 人工接管**。发送 `dy_remote_login`，管理员在真实 Chromium 中完成扫码、短信或任意滑块验证。AstrBot 通过 CDP 复用同一个浏览器和持久化 profile，每 2 秒检查抖音页面是否已确认登录。适合 Linux/Docker 环境，也适合自动滑块持续返回“操作过快 [5014][502]”的情况。
- **自动二维码截图**。发送 `dy_qr`，插件自动等待二维码、分段处理拼图滑块，并在验证框消失后固定等待 5 秒再截取新二维码发回当前会话。

`dy登录` 在配置了 `browser_cdp_url` 时自动进入 noVNC 人工登录；未配置时沿用 `dy_qr` 的自动截图流程。登录、二维码和 Cookie 命令仅限 AstrBot 管理员。

### Docker / Docker Compose 人工登录

项目自带 `docker-compose.browser.yml`，会启动一个独立的 Chromium、Xvfb、x11vnc 和 noVNC 服务。Chromium 只在 AstrBot 的 Docker 网络内开放 CDP 端口，noVNC 默认只绑定服务器的 `127.0.0.1:6080`。

1. 确认 AstrBot 容器的 Docker 网络名称：

   ```bash
   docker network ls
   docker inspect astrbot --format '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}}{{"
"}}{{end}}'
   ```

   默认网络名是 `astrbot_default`。如果 Compose 项目使用了自定义名称，请在下一步的 `.env.browser` 中修改 `ASTRBOT_NETWORK`。

2. 在插件目录创建浏览器服务环境文件并修改 VNC 密码：

   ```bash
   cp .env.browser.example .env.browser
   # 编辑 .env.browser，将 VNC_PASSWORD 改成自己的密码
   ```

   VNC 密码最多 8 个字符。保持 `VNC_BIND_ADDR=127.0.0.1`，不要把 `6080` 直接映射到公网。

3. 启动并检查浏览器服务：

   ```bash
   docker compose --env-file .env.browser -f docker-compose.browser.yml up -d --build
   docker compose --env-file .env.browser -f docker-compose.browser.yml ps
   docker compose --env-file .env.browser -f docker-compose.browser.yml logs -f douyin-browser
   ```

   Chromium 的登录资料保存在 Docker named volume `douyin-browser-profile` 中。更新镜像或重建容器不会清除登录态。

4. 在 AstrBot 插件面板配置：

   ```text
   browser_cdp_url=http://douyin-browser:9222
   browser_vnc_url=http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=scale
   browser_manual_timeout=600
   browser_channel=
   ```

   `browser_cdp_url` 是 AstrBot 容器访问浏览器服务的**容器内地址**，不要改成宿主机 IP，也不要公开 `9222`。

5. 从自己的电脑建立 SSH 隧道，把服务器的本地 noVNC 端口映射到本机：

   ```bash
   ssh -L 6080:127.0.0.1:6080 user@server
   ```

   然后在本地浏览器打开：

   ```text
   http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=scale
   ```

6. 给机器人发送 `dy_remote_login`，在 noVNC 页面输入 VNC 密码，完成抖音的扫码、短信或滑块验证。插件会自动确认登录并保存登录态；超时后浏览器和登录页仍会保留，可以继续操作，也可以重新发送命令。

如果使用 HTTPS 反向代理远程访问 noVNC，必须在代理层增加独立身份认证和访问控制。noVNC 的 VNC 密码不能替代公网访问认证，`6080` 和 `9222` 都不应直接暴露到公网。

### 自动截图登录

`dy_qr` 默认使用 `browser_mode=auto`。Linux 无显示环境时，插件会启动独立 Xvfb；如果部署在 Docker 中，这些系统依赖必须安装在 **AstrBot 容器镜像内**，只在宿主机安装不会生效。

遇到拼图滑块时，插件会识别缺口并按显示尺寸换算距离，分四段拖动。每段滑动后保持按住并停顿约 1.5 秒，到达终点并停顿后再松开，兼容 iframe 内的验证页。页面确认验证框消失后，固定等待 5 秒，再重新截图；只有能实际解码的二维码才会发送，验证码页、占位图和下载 App 的二维码不会被当成登录二维码。

准备二维码和等待扫码分别计时：`qr_prepare_timeout` 默认 180 秒，`qr_scan_timeout` 默认 150 秒。二维码过期会自动刷新并推送新图。自动滑块默认最多尝试 `qr_slider_attempts=3` 次；明确返回“操作过快 [5014][502]”时会停止本轮提交。旋转、点选等无法可靠识别的验证类型请改用 `dy_remote_login`。

### 登录态判定与限制

登录态必须同时具备抖音域名下未过期的 `sessionid` / `sessionid_ss`，并由抖音页面确认 `LOGIN_STATUS=1`、`localStorage.HasUserLogin=1` 或已登录头像。`dy状态` 会区分“页面已确认”“仅有 Cookie”“未登录”。只有 Cookie 不代表服务端会话仍然有效，应重新执行 `dy_remote_login`。

`dy_cookie <整段 Cookie>` 仍可用于导入 `a=1; b=2` 格式，但后续评论和互动同样依赖浏览器上下文。Cookie、二维码和 `login_cookies.json` 都包含登录凭证，只能发送给自己管理的会话，不应分享或提交到 Git。

noVNC 人工接管能处理自动流程无法识别的验证，但抖音仍可能拒绝特定服务器 IP、地区或网络。插件无法绕过平台针对账号、IP 或地区的服务端限制；遇到持续拒绝时，应更换合规的网络环境或改用本地部署。

从 1.3.7 升级时，旧 `qr_cdp_port` 配置会保留但不再启用。自动截图和 noVNC 流程都不需要公开旧的远程调试端口。

## 依赖

- Python 依赖见 `requirements.txt`（Playwright、aiohttp、OpenCV headless、NumPy、Pillow、imageio-ffmpeg）
- 系统浏览器：Edge（默认）或 Playwright Chromium（`playwright install chromium`）
- Docker 人工登录：使用项目自带的 `docker-compose.browser.yml`，不需要在 AstrBot 镜像内额外安装 noVNC
- 视觉模型：AstrBot 服务商里任一支持图片输入的模型（Gemini / Doubao / GPT-4o 等）

## 配置项（面板）

**基本**

- `enabled`：总开关
- `trigger_words`：触发词 list
- `sub_short_ids`：订阅抖音号 list（抖音号 / 房间号 / 链接均可，自动解析）
- `forward_groups`：开播推送群号 list
- `reply_text` / `not_live_text`：开播配文 / 全未开播回复
- `poll_interval`：轮询间隔秒（默认 300）
- `fetch_cover`：带封面图

**陪看评论**

- `comment_enabled`：评论开关
- `frame_count`：抽帧张数（默认 6）
- `comment_style`：评论风格提示词
- `cooldown_hours`：同视频冷却小时（默认 24）
- `global_cooldown_minutes`：全局评论间隔分钟（默认 30）
- `notify_groups`：备用推送群号 list（`forward_groups` 没配时的兜底）

**视觉模型**

- `vision_provider_id` / `vision_model`：指定提供商和模型，留空用当前默认
- `vision_fallback_provider_ids`：主模型挂了的回退顺序
- `vision_timeout_seconds`：看图超时秒（默认 120）

**登录与浏览器**

- `ttwid`：可选，降低风控
- `browser_channel`：`msedge`（默认）或留空用 Chromium
- `browser_mode`：`auto`（默认，普通浏览器 + 按需 Xvfb）、`headed`（同样按需提供显示）、`headless`（无头）；修改后重载插件
- `browser_cdp_url`：独立服务器 Chromium 的容器内 CDP 地址，例如 `http://douyin-browser:9222`；留空使用插件自己的浏览器
- `browser_vnc_url`：管理员访问 noVNC 的完整地址；远程使用应通过 SSH 隧道或带身份认证的 HTTPS 反向代理
- `browser_manual_timeout`：`dy_remote_login` 等待人工验证的秒数，默认 600，范围 120-1800
- `qr_prepare_timeout`：等待二维码和验证的超时秒数，默认 180
- `qr_scan_timeout`：首张二维码发送后等待扫码的秒数，默认 150
- `qr_slider_attempts`：自动处理拼图滑块的最大次数，默认 3
- `qr_cdp_port`：旧配置兼容字段，已停用

**主动刷视频**

- `proactive_enabled`：主动刷开关（也可 `dy主动 开/关`）
- `proactive_interval`：轮询间隔**分钟**（默认 15；旧配置存的秒值 >1440 会自动折算，1.3.4 起单位从秒改为分钟）
- `proactive_batch`：每轮看几个（默认 3）
- `score_threshold`：评论打分门槛 0-10（默认 7，0=全评）
- `proactive_comment`：过线后是否自动评论（关=只分享不评）
- `taste_words`：口味关键词 list（打分加权，`dy口味` 快捷设置）
- `master_user_id`：主人 QQ 号（备用；正常推送都走直播转发群）

**互动**

- `interact_enabled`：互动轮询开关（`dy互动 开/关`）
- `interact_interval`：轮询间隔秒（默认 300）
- `dm_notify`：私信新消息提醒
- `reply_notify`：评论被回复提醒

## 数据文件（插件 data 目录）

- `douyin_live.json`：订阅 / 转发群 / 特别关心
- `cooldown.json`：评论冷却记录
- `seen_videos.json`：主动刷看过/评过的视频（30 天）
- `interact_state.json`：互动已处理记录（30 天）
- `browser_profile/`：插件自管浏览器的持久化 profile（登录态）
- `login_cookies.json`：登录成功时保存的会话 Cookie，供重启恢复
- Docker named volume `douyin-browser-profile`：noVNC 浏览器服务的持久化 profile（登录态）

## 开发验证

```bash
python -m pip install -r requirements-dev.txt
python -m playwright install chromium
python -m pytest -q
ruff check main.py core/auth.py core/browser.py core/captcha.py core/display.py core/login_capture.py tests
```

浏览器测试默认使用 Chromium；测试机使用 Edge 时，设置环境变量 `PLAYWRIGHT_TEST_CHANNEL=msedge`。测试页及 Cookie 都是本地构造的数据，覆盖 iframe 分段拼图拖动、5 秒延时、二维码占位、发图时序、超时/取消、CDP 上下文复用、人工登录、状态判定、虚拟显示生命周期和重启恢复，不会登录真实账号。v1.4.1 共 40 项测试通过；线上检查与尚未验证的环境见 [VALIDATION.md](VALIDATION.md)。

## 交流群

QQ 群：`1108937527`（有问题/建议群里说，代码 issue 走 GitHub）

## License

MIT，见 [LICENSE](LICENSE)。

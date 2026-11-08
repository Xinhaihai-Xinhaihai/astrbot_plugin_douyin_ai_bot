# astrbot_plugin_douyin_ai_bot

抖音 AI 陪看插件（AstrBot v4）

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

- **直播订阅检测**：订阅主播直播间，定时轮询开播状态，开播自动推「封面图 + 直播间链接 + 配文」到指定群，群里说触发词即时查
- **陪看评论**：发个抖音视频链接 → 下载抽帧喂多模态视觉模型「看懂」→ 用登录身份发一条像真人的短评 → 截图推转发群（私聊只回一行文字确认，不再刷屏）
- **主动刷视频**：定时刷一批视频（特别关心 up 优先）→ 逐个打分 → 过线才评论 → 截图推直播转发群
- **互动提醒**：轮询私信新消息、我发的评论被谁回复了，推直播转发群；支持一键回私信
- **服务器扫码登录**：持续截图等待二维码 / 自动尝试拼图滑块 / 验证通过后等 5 秒截码推送；也可导入浏览器 Cookie

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
| `dy登录` | 管理员获取登录二维码，与 `dy_qr` 相同，支持无桌面服务器 |
| `dy_qr` | 管理员扫码登录：自动尝试拼图滑块，通过后等 5 秒截码并发回当前会话 |
| `dy_cookie <整段Cookie 或发txt文件>` | 浏览器登录抖音后 F12-Cookie 全选复制，贴过来导入 |
| `dy状态` | 登录态 / 订阅 / 转发群 |
| `添加触发词 <词>` / `删除触发词 <词>` / `触发词列表` | 触发词管理 |
| 触发词（默认：看看腿） | 即时检查直播，有在播的发封面+链接+配文 |

## 服务器登录

`dy登录` 和 `dy_qr` 使用同一条截图登录流程，不需要远程桌面或开放调试端口。登录、二维码及 Cookie 导入命令仅限 AstrBot 管理员。

默认 `browser_mode=auto` 使用普通浏览器。Linux 无显示环境时，插件自动启动独立 Xvfb 虚拟显示，浏览器仍在服务器上运行，二维码自动截图发到聊天中。已有 `DISPLAY` 时复用现有显示；Windows/macOS 使用本机浏览器窗口。

无头浏览器中的“操作过快 [5014][502]”不一定由拖动速度引起：分段等待后仍可能被服务端拒绝。普通浏览器模式是本次增加的可选运行路径，不能保证所有服务器网络环境都通过验证。

1. 在 **AstrBot 运行环境内**安装依赖与浏览器；Docker 部署需在容器内执行，并持久化 AstrBot 的 `data` 目录：

   ```bash
   # Debian/Ubuntu；系统依赖安装需使用有权限的账户。
   apt-get update
   apt-get install -y xvfb
   python -m pip install -r requirements.txt
   python -m playwright install --with-deps chromium
   ```

2. 在插件配置中将 `browser_channel` 留空、`browser_mode` 设为 `auto`，使用 Playwright Chromium；已有 Edge 可保留 `msedge`。修改模式后重载插件。Linux 缺少 Xvfb 会收到安装提示；确实需要无头模式时可显式设置 `headless`，此时不需要 Xvfb。
3. 给机器人发送 `dy_qr`。插件在后台打开抖音并持续检查截图；遇到拼图滑块会识别缺口、换算显示尺寸并分四段拖动。每段滑动后保持按住，停顿 **1.5 秒**，到达终点并停顿后再松开提交，兼容 iframe 内的验证页。
4. 页面确认验证框消失后，**固定等待 5 秒**，然后重新截图。只有二维码能实际解码，才将二维码裁剪图发回发起命令的会话；尚未出现则继续等待。无滑块时，二维码就绪即可发送。
5. 用手机抖音扫码并确认。浏览器在发图后保持打开，默认再等 150 秒；二维码过期后自动刷新并推送新图。登录成功会通知并保存登录态。

准备二维码和等待扫码分别计时，配置为 `qr_prepare_timeout`（默认 180 秒）和 `qr_scan_timeout`（默认 150 秒）。一次只允许一个扫码任务，重复命令会提示当前仍在登录。插件卸载会取消登录任务并清理浏览器。

拼图滑块默认最多尝试 3 次（`qr_slider_attempts`）。普通识别或拖动失败可重试；如果页面明确返回“操作过快 [5014][502]”，本轮立即停止提交，避免对已拒绝的会话反复刷新。自动处理需要拼图结构可识别；旋转、点选等类型不保证支持。失败会明确提示，不会把验证页或占位图当成二维码发送。可调整运行模式后重试，或使用 `dy_cookie` 导入自己浏览器的 Cookie。

Docker 部署须在 **AstrBot 所在容器内**安装 `xvfb` 和 Chromium 的系统依赖，在镜像构建中保留这些安装步骤；只在宿主机安装不会生效。插件管理自己的 Xvfb 进程，无需另开 VNC、无需 `xvfb-run` 包裹整个 AstrBot，也不会修改其他插件的 `DISPLAY`。卸载时会关闭其创建的显示进程。

`dy_cookie <整段 Cookie>` 支持 `a=1; b=2` 格式。Cookie、二维码只应发送到自己管理的会话。导入 Cookie 同样需要服务器浏览器，因为后续功能使用浏览器上下文。

登录态保存在插件数据目录中的 `browser_profile/` 及 `login_cookies.json`。后者补充保存会话 Cookie，以便重启恢复；这些文件包含登录凭证，不应分享。是否登录按抖音域名下未过期的 `sessionid` / `sessionid_ss` 判断，服务端撤销的会话仍需重新登录。

从 1.3.7 升级时，旧 `qr_cdp_port` 配置会被保留但不再启用，`dy_qr` 始终走自动截图流程。

## 依赖

- Python 依赖见 `requirements.txt`（Playwright、aiohttp、OpenCV headless、NumPy、Pillow、imageio-ffmpeg）
- 系统浏览器：Edge（默认）或 Playwright Chromium（`playwright install chromium`）
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
- `browser_profile/`：浏览器持久化 profile（登录态）
- `login_cookies.json`：登录成功时保存的会话 Cookie，供重启恢复

## 开发验证

```bash
python -m pip install -r requirements-dev.txt
python -m playwright install chromium
python -m pytest -q
ruff check main.py core/auth.py core/browser.py core/captcha.py core/display.py core/login_capture.py tests
```

浏览器测试默认使用 Chromium；测试机使用 Edge 时，设置环境变量 `PLAYWRIGHT_TEST_CHANNEL=msedge`。测试页及 Cookie 都是本地构造的数据，覆盖 iframe 分段拼图拖动、5 秒延时、二维码占位、发图时序、超时/取消、虚拟显示生命周期和重启恢复，不会登录真实账号。线上检查与尚未验证的环境见 [VALIDATION.md](VALIDATION.md)。

## 交流群

QQ 群：`1108937527`（有问题/建议群里说，代码 issue 走 GitHub）

## License

MIT，见 [LICENSE](LICENSE)。

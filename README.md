# astrbot_plugin_douyin_ai_bot

抖音 AI 陪看插件（AstrBot v4）

- **直播订阅检测**：订阅主播直播间，定时轮询开播状态，开播自动推「封面图 + 直播间链接 + 配文」到指定群，群里说触发词即时查
- **陪看评论**：发个抖音视频链接 → 插件下载视频抽帧喂多模态视觉模型「看懂」内容 → 用登录身份发一条像真人的短评 → 渲染可爱风截图推群。不点赞、不刷量，评论频率受「同视频冷却 + 全局间隔」双重限制
- **三种登录方式**：本机浏览器扫码 / 推二维码图手机扫 / 直接贴浏览器 Cookie 导入，cookie 落盘后长期复用

## 直播检测原理

- 19 位房间 id（手机分享链接解析出来的）→ 直接 HTTP 拉 reflow 分享页判断，无需浏览器，秒出结果
- 12 位左右的直播间号（live.douyin.com 地址里的）→ Playwright 无头浏览器渲染页面判断
  - 开播：页面含「在线观众 / 本场点赞」或标题含「xxx的抖音直播间」
  - 未开播：页面含「未开播 / 暂未开播 / 直播回放」

抖音 web 接口（webcast enter）带签名校验，直接 HTTP 调用会被拒，因此直播间号走页面渲染最稳。
默认使用系统 Edge（`msedge`），无需额外下载浏览器；也可留空用 Playwright Chromium（需先 `playwright install chromium`）。

### 订阅对象怎么填

1. **最省事（推荐）**：手机抖音 App 进主播直播间 → 右上角「…」→ 分享 → 复制链接，把整段分享文本（形如 `#在抖音，记录美好生活#【xxx】正在直播… https://v.douyin.com/xxxx/ …`）直接粘给机器人订阅，插件自动展开短链拿房间号。
2. **直播间号**：电脑浏览器打开正在播的直播间，地址栏 `https://live.douyin.com/123456789012/` 里的数字。
3. **抖音号**：`订阅 Lianmin726` 这样直接填抖音号也行，会自动用无头浏览器定位直播间，并把这个抖音号当昵称做开播校验（页面渲染到别人则判未开播）。

## 陪看评论原理

1. 解析视频链接里的 aweme_id（支持 v.douyin.com 短链和完整链接）
2. HTTP 拿视频信息 + 下载地址，ffmpeg 按 `frame_count` 均匀抽帧
3. 截图喂视觉模型（配 `comment_style` 提示词）生成 1-2 句口语短评
4. 带登录 cookie 调评论接口发出（需先登录，见下）
5. 把「视频首图 + 评论 + Cinnamoroll 相框」渲染成截图，推给触发者 / 截图推送群

限制：同一视频 `cooldown_hours` 内只评一次；任意两次评论间隔 ≥ `global_cooldown_minutes`。

## 命令

| 命令 | 说明 |
| --- | --- |
| `订阅 <抖音号/直播间号/分享整段文本>` | 订阅主播 |
| `取消订阅 <抖音号>` | 取消 |
| `订阅列表` | 查看订阅及当前开播状态 |
| `直播转发 <群号>` / `取消转发 <群号>` | 开播推送群管理 |
| `dy评论 <视频链接>` | 看视频→发短评→截图推送 |
| `dy登录` | 拉起本机浏览器扫码（180s 超时） |
| `dy_qr` | 无头浏览器生成登录二维码推给你，手机扫（150s 有效），服务器不用开浏览器窗口 |
| `dy_cookie <整段Cookie 或发txt文件>` | 浏览器登录抖音后 F12-应用程序-Cookie 全选复制，贴过来直接导入 |
| `dy状态` | 查登录态 / 订阅 / 配置 |
| `添加触发词 <词>` / `删除触发词 <词>` / `触发词列表` | 触发词管理（一个个加，别用逗号） |
| 触发词（默认：看看腿） | 即时检查，有在播的发封面+链接+配文，全没播回「都还没开播呢」 |

> 登录只走浏览器/Cookie，没有账号密码登录（抖音 web 扫码登录需要 a_bogus 签名，密码登录不开放）。

## 依赖

- Python 依赖见 `requirements.txt`（playwright、aiohttp、ffmpeg 二进制）
- 系统浏览器：Edge（默认）或 Playwright Chromium
- 视觉模型：AstrBot 服务商里任一支持图片输入的模型

## 配置项（面板）

**基本**

- `enabled`：总开关
- `trigger_words`：触发词 list
- `sub_short_ids`：订阅抖音号 list（填抖音号 / 房间号 / 链接均可，自动解析）
- `forward_groups`：开播推送群号 list
- `reply_text` / `not_live_text`：开播配文 / 全未开播回复
- `poll_interval`：轮询间隔秒（默认 300）
- `fetch_cover`：带封面图

**陪看评论**

- `comment_enabled`：评论开关
- `frame_count`：抽帧张数（默认 6，越多越懂越费 token）
- `comment_style`：评论风格提示词
- `cooldown_hours`：同视频冷却小时（默认 24）
- `global_cooldown_minutes`：全局评论间隔分钟（默认 30）
- `notify_groups`：评论截图推送群号 list

**视觉模型**

- `vision_provider_id` / `vision_model`：指定提供商和模型，留空用当前默认
- `vision_fallback_provider_ids`：主模型挂了的回退顺序
- `vision_timeout_seconds`：看图超时秒（默认 120）

**登录与浏览器**

- `ttwid`：可选，登录后 Cookie 里 ttwid 的值，填了更不容易被风控拦
- `browser_channel`：`msedge`（默认）或留空用 Chromium

## 数据

- `douyin_live.json`：订阅 / 转发群
- `cooldown.json`：评论冷却记录
- `browser_profile/`：浏览器持久化 profile（登录态存在这里）

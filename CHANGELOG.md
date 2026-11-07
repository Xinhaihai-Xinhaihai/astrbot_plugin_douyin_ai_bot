# 更新日志

## v1.3.7
- 新增：`dy_qr` 支持「浏览器扫码端口」登录——配了 `qr_cdp_port`（默认 0=关闭）后，`dy_qr` 会起带远程调试端口的无头浏览器打开抖音，你浏览器访问 `服务器IP:端口` 进去扫码，扫完自动落盘登录态。解决抖音把无头二维码截图拦成「验证码中间页」（二维码全是实心方块占位）的问题
- 配置：新增 `_auth_section.qr_cdp_port`（int，0-65535），README 登录方式表和配置说明同步更新

## v1.3.6
- 调整：过线视频截图不再渲染封面图（接口 cover 是占位图 + 占版面），直接从标题开始排版

## v1.3.5
- 开源：加 MIT LICENSE；README 加 license 徽章和 License 章节

## v1.3.4
- 修复：三条视频封面一模一样——接口拿到的 cover 是抖音占位图，改成抽帧优先（首帧才是该视频真正的封面图），接口封面只作无帧时兜底
- 调整：`proactive_interval` 单位从秒改为**分钟**（默认 15，面板滑块 1~1440）；旧配置存的秒值（>1440）自动折算，不用手动改

## v1.3.3
- 修复：打分汇总卡片里标题/作者/简介全空——抖音视频页改版后 DOM 不再渲染 `video-desc`/`account-name`，改为拦截页面自身的 `aweme/detail` 接口拿简介、作者、点赞/评论/收藏数，`comment/list` 接口拿热评，DOM 只作兜底（mp4 抽帧不受影响）

## v1.3.2
- 修复：主动刷视频过线触发 `dy主动 刷` 崩（`unsupported operand type(s) for -: 'float' and 'dict'`）——`SeenStore._save` 裁剪时 `commented` 存的是 `{"ts","score","comment"}` dict，直接和 `time.time()` 相减炸了，现在按 `ts` 取时间戳裁剪，顺便兼容脏数据

## v1.3.1
- 修复：新版 AstrBot 的 `MessageChain` 没有 `.image()` 方法导致 `dy主动` 等命令直接崩（`'MessageChain' object has no attribute 'image'`），7 处全部换成新 API：本地图 `file_image()`、网图 `url_image()`

## v1.3.0
- 修复：推荐流/用户主页抓不到视频 ID 的问题——抖音首页卡片链接不挂在 `<a href>` 上，增加 HTML 正文正则兜底（`/video/(\d{15,21})`），登录态正常时稳定出候选

## v1.2.0
- 修复：7 个 core 模块 logger 统一改为 `from astrbot.api import logger`（审核驳回项）
- 修复：浏览器 channel 降级失败时抛真实错误，不再静默吞异常
- 调整：订阅流/推荐流候选收集拆成独立 try/except，推荐流挂了不影响订阅抓取
- 新增：候选为空时推群提示「可能登录态掉了，试试 dy_qr」
- 调整：`fetch_feed_videos` / `fetch_user_videos` 必须由调用方传入带 channel 的浏览器实例，禁止内部裸建（修 headless chromium 找不到崩溃）
- 调整：`dy评论` 截图改推转发群（`forward_groups`），私聊只回一行文字确认，不再每天私聊塞图
- 调整：所有群推送统一走 forward_groups → notify_groups 兜底（dy评论 / 主动刷过线 / 打分汇总 / 互动提醒 同一出口）

## v1.1.1
- 修复：打分汇总卡片文字重叠（循环漏推进 y 坐标）
- 修复：截图里 ♥/♡/✿/▸ 等微软雅黑不含的字形显示成方块，全部换成 ★/◆/→

## v1.1.0
- 新增：主动刷完一轮若没有视频过线，推一张「本轮打分汇总」卡片（和过线截图同一个相框）：每个视频的标题 / 作者 / 分数 / 打分理由 / 链接，方便看清模型看了啥、为什么没过线
- 有视频过线时不推汇总（过线的各自单推过截图）

## v1.0.1
- 修复：main.py 里 6 处 `from .browser` / `from .douyin` 补全为 `.core.*`，修复 `dy主动` 等命令运行时 `No module named 'data.plugins....browser'`
- 新增：全部 19 个命令补上描述（面板命令列表不再显示"无描述"）
- 新增：`dy登录` 自动识别无桌面环境（服务器/Console 会话），直接改推二维码图手机扫，不再傻开浏览器窗口
- 调整：主动刷过线视频 / 私信 / 评论回复提醒，统一推直播转发群（`forward_groups`），不再推私聊
- 修复 metadata.yaml：help 文案（原先是从别的插件抄来的错误命令）、version 改语义化、补 tags/social_link
- README 重写为 ai_bot 全功能文档

## v1.0.0
- 从 douyin_live 改造而来：保留直播订阅检测，新增 AI 陪看评论 / 主动刷视频 / 互动提醒
- 三种登录方式：dy登录（本机浏览器）/ dy_qr（推二维码）/ dy_cookie（贴浏览器 Cookie）
- 陪看评论：抽帧喂多模态视觉模型生成真人短评，登录身份发出，渲染截图推群
- 主动刷视频：定时刷候选（特别关心 up 优先）→ 视觉模型打分 → 过线才评
- 互动提醒：轮询私信/评论回复，支持一键回私信
- 评论频率受同视频冷却 + 全局间隔双重限制

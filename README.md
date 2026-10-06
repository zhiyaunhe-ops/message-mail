# message-mail — 本地邮件 Web 阅读器 + AI 可读 API

把 IMAP 邮箱收件箱搬到本地一份可搜索的 SQLite 索引，同时提供：
- **WebUI**：5 套美学主题（`/?theme=ink|swiss|neon|terminal|glass`），三栏布局，搜索 / 标记已读 / 附件下载；窄屏（≤900px）自动切成单栏 —— 目录变成一条可横滑的胶囊、阅读区整屏覆盖带「‹ 返回列表」、弹窗铺满全屏，手机上也能用
- **会话视图**：列表头可切换「邮件 / 会话」——把往来的多封邮件聚成一条 IM 风格的聊天（收件箱与已发送自动合并、引用历史折叠、自己发的靠右）
- **无聊邮件分类**：本地规则引擎自动打标（📅 会议/日程、🤖 系统自动、📢 营销推广），列表徽章显示、可一键「只看人工」
- **写邮件 / 回复全部 / 转发 / 删除**：顶栏「✏ 写邮件」（SMTP，支持多附件）；附件加进列表后可以一个个移除（也可以直接拖文件进写信框）；阅读器与会话气泡可「↩ 回复」「↩↩ 回复全部」（发件人+原收件人+原抄送，自动去重并剔除自己）、「↪ 转发」和「🗑 删除」（COPY 到服务器回收站）。三种入口在会话气泡上也能用，转发的是该会话最新一封
- **回复 / 转发引用**：发出去的是 `multipart/alternative` —— 正文另有一份 HTML，引用块用**标准英文引用头**（`From / Sent / To / Cc / Subject`，与收件方的 Outlook 一致）加缩进块，原信里的内嵌图（cid）转成真正的内联图随信发出；纯文本分支仍是 `-----Original Message-----` + `> ` 前缀兜底。碰到上一轮的引用就停（不套娃，HTML 与纯文本都切在同一处）。**上下文会随草稿一起存在服务器上**（回复是 `X-Message-CLI-Reply-*`，转发是 `X-Message-CLI-Forward-*`），所以关掉写信框、从草稿箱「继续编辑」再发，仍然是那封回复/转发而不是新邮件。两者刻意不同：主题前缀分别是 `Re:` / `Fwd:`（各自认得出已有的 `RE[2]:` / `FW:`，不叠前缀），**只有回复才挂 `In-Reply-To` / `References`**，转发不挂——否则会被收件方并回原会话；转发还会把原信的非内嵌附件一并带上（合计超过 25MB 会提示并跳过）
- **AI 写正文**：读与收件人的最近往来邮件 + 当前草稿，给出「正式得体 / 简洁高效 / 委婉推进」3 个方案，每个方案还带一个可选主题（默认只在主题为空时补上，也可勾选覆盖）；面板收起后点「✨ AI 建议」能再展开，套用不丢
- **收件人 / 抄送联想**：收件人、抄送都是一个一个「框」（chip），输入姓名或邮箱即按历史邮件做相近联想（`↑↓` 选择、`回车`确认、`退格`删框、粘贴一整串地址自动拆成多个框）；联想结果优先给「我写过信的人」，营销/系统推送降权
- **收件人写错不会静默丢件**：显示名里带逗号的（`Sample, Steven <a@b.com>` 这种，本地邮箱里很常见）写出去会自动加引号，否则会被解析成两个人；只有名字没邮箱的串不会被投递，而是如实回报「被忽略的收件人」；整串都没有效地址、或只剩自己时，报错会点明是哪个地址、怎么改，失败原因也写进同步日志
- **多邮箱账号**：账号写在 `config/himalaya/config.toml`，每个账号各自独立的本地索引与收件箱，顶栏随时切换，邮箱管理弹窗里增删改并测试连接
- **同步**：顶栏「同步」按筛选框的时间范围拉取（近 1 月 / 近 7 天 / 全部时间 / 自定日期界线）；另有一份后台自动同步兜底——默认**每 10 分钟同步近 1 天**、**启动服务时同步近 1 周**，间隔与回溯天数都在 ⚙ 设置里改，改完立即生效不用重启
- **REST API**：完整的 JSON 接口；自动生成 OpenAPI 文档（`/docs`）
- **引用头时区**：回复 / 转发写在引用头里的 `Sent: Monday, 15 September 2026 09:02` 默认按**服务进程所在时区**换算（前端列表也是按读者本地时区显示，口径一致）。跑在容器或别的时区主机上时进程时区并不等于读者时区，会静默偏几个小时 —— 这时在 ⚙ 设置（或 `config/app.toml` 的 `[display] utc_offset`）填固定偏移（`"+08:00"` / `"+8"` / `"-07:00"`），下一次发信就生效、不用重启；填错会当场 400 并说明写法。收的是偏移而不是 `Asia/Hong_Kong` 这类名字：Windows 没有系统 tz 数据库，`zoneinfo` 解析 IANA 名字要额外装 `tzdata`，为一个显示设置不值得加依赖。原文没带偏移的（只有邮件无 `Date:` 头、退回 IMAP `INTERNALDATE` 那条兜底会这样）**不猜时区、原样显示** —— 按进程时区再折一次等于凭空发明一个偏移量
- **AI 友好端点**：结构化 JSON、自动截断正文、含 `attachments` 直链；详见 `/llms.txt` 与 `/api/ai/schema`

> 本项目由原 `Message-cli/email-only` 分支独立拆出，现作为单独仓库维护；这里只保留邮件相关能力。

## 启动

```bash
# 1. 建虚拟环境并安装依赖（.venv 已在 .gitignore 中）
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt          # Windows
# python3 -m venv .venv && .venv/bin/pip install -r requirements.txt # macOS / Linux

# 2. 配置邮箱：复制模板后填入自己的账号与密码
cp config/himalaya/config.example.toml config/himalaya/config.toml
cp config/himalaya/secret.example     config/himalaya/secret      # 写入 IMAP 密码

# 3. 同步邮件到本地索引（首次约 30s；--since 不传 = 近 1 月）
python run.py sync --all-folders

# 4. 启动 WebUI + API（默认 127.0.0.1:8791）
python run.py serve --host 127.0.0.1 --port 8791
```

副命令：`sync` / `verify` / `ai` —— 见 `python run.py -h`。

> 下文所有 `python` 都指项目内 `.venv` 的解释器：Windows 直接写 `.venv\Scripts\python.exe`，
> macOS / Linux 先 `source .venv/bin/activate`。托盘/启动脚本会自动优先使用 `.venv`（见文末）。

密码优先级：`MAIL_PASSWORD` 环境变量 > 配置里的 `password.command` > `config/himalaya/secret` 文件。
真实的 `config.toml` 与 `secret` 都已 gitignore，仓库里只有 `.example` 模板。

## 服务结构（单进程）

**只有一个服务**：邮件跑在一个 FastAPI 进程、一个端口上。

```
app/
├── main.py          装配层：app 实例 / CORS / 静态资源 / 首页 / 生命周期
├── context.py       ★ 统一运行上下文：账号、SQLite Store、IMAP 客户端、
│                     同步进度与日志、TTL 缓存
├── api_mail.py      邮件路由：目录 / 列表 / 详情 / 附件 / 发信 / 会话 / 分类 / AI 端点
├── api_accounts.py  多邮箱账号管理路由
├── api_ai.py        AI 写正文 + 同步/展示设置路由
├── ai.py            OpenAI 兼容 /chat/completions 调用（AI 写正文）
├── classify.py      无聊邮件分类（规则引擎）
├── threads.py       会话归组（并查集）
├── imap_client.py   IMAP 封装（读取 + 删除 + APPEND 已发送）
├── mailout.py       SMTP 发信
├── mailparse.py     MIME 解析（GBK 兼容 / cid 内嵌资源）
├── quoting.py       回复/转发引用块（标准引用头 + HTML / 纯文本两种引用体）
├── store.py         SQLite 索引
├── sync.py          增量同步
├── autosync.py      后台同步线程：启动补一次 + 按间隔轮询
├── appconfig.py     应用级配置（config/app.toml：[ai] / [sync] / [display]）
└── settings.py      配置与密码读取
```

- 统一状态在 `app/context.py` 的 `ctx`：`account / store / client / syncing / last_sync / log / 缓存`，
  缓存带模块前缀，可精确失效
- 前端只有一个全局 `state`（列表/阅读/写信上下文）

## 主题

| id | 主题 | 关键风格 |
| --- | --- | --- |
| ink | 宣纸水墨 | 宋体 × 朱砂 × 宣纸纹理 |
| swiss | 瑞士极简 | 黑白网格 × 国际主义排版 |
| neon | 暗夜霓虹 | 深空底 × 紫青辉光 |
| terminal | 复古终端 | CRT 绿字 × 扫描线 |
| glass | 琉璃柔彩 | 毛玻璃 × 粉紫渐变 |

主题选择持久化在 localStorage，也可通过 URL `?theme=xxx` 强制指定。

## 关键 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET  | `/api/ai/inbox` | AI 首选：结构化邮件列表，正文按 `body_chars` 截断 |
| GET  | `/api/ai/threads` | AI 会话视图（同一来回复用一封，聊天式输出） |
| GET  | `/api/ai/digest` | 按 sender/date/subject/folder 聚合统计 |
| GET  | `/api/ai/verify` | 校验时间窗内各目录数量与日期范围 |
| GET  | `/api/status` | 服务与索引状态（目录计数 / 同步状态 / 自动同步） |
| GET  | `/api/folders` | 目录列表与计数（`refresh=1` 强制回源 IMAP） |
| GET  | `/api/messages` | 信封级列表（含 q/since/unread_only/has_attachment 过滤） |
| GET  | `/api/messages/{uid}` | 单封全文（headers / body_text / body_html / attachments） |
| GET  | `/api/messages/{uid}/attachment/{idx}` | 下载附件 |
| POST | `/api/messages/{uid}/flags` | 标记已读 / 未读 / 星标 |
| DELETE | `/api/messages/{uid}` | 删除（移入服务器回收站） |
| POST | `/api/send` | 发信（SMTP，多附件；回复 / 转发上下文） |
| POST | `/api/drafts` | 存草稿 / 替换草稿 |
| GET  | `/api/threads` | 会话列表（IM 风格） |
| GET  | `/api/threads/{thread_id}` | 单条会话全文 |
| GET  | `/api/contacts` | 联系人联想 |
| POST | `/api/sync` | 增量同步（`since` 不传 = 近 1 月；撞车返回 429） |
| GET/PUT | `/api/accounts…` | 多邮箱账号管理 + 连接测试 |
| POST | `/api/compose/ai` | AI 写正文：3 个场景方案 |
| GET  | `/api/config/sync` | 读同步策略 + 自动同步状态（间隔 / 回溯天数 / 下一轮倒计时） |
| PUT  | `/api/config/sync` | 改同步策略（立即生效，不用重启） |
| POST | `/api/config/sync/now` | 立刻跑一轮自动同步 |
| GET/PUT | `/api/config/ai` | AI 接口配置（密钥打码；`/test` 测连通性） |
| GET  | `/api/config/display` | 读引用头时区（`utc_offset` / `effective` / `source`） |
| PUT  | `/api/config/display` | 改引用头时区偏移（填错返回 400 并说明写法；下一次发信生效，不用重启） |
| GET  | `/llms.txt` | 给 AI 看的接口说明 |
| GET  | `/api/ai/schema` | JSON 版接口自述 |

## MCP 服务器（`mcp_server.py`）—— AI 客户端接入

把上面这堆 REST API 收敛成 **7 个精挑工具**（6 只读 + 1 写）的 MCP server（stdio），给 ZCode / Claude 等客户端用。

- **运行方式**：独立小进程，只依赖 `fastmcp` + `httpx`（与 WebUI 的 `.venv` 无关）；所有状态仍在 Web 服务手里，本进程只转发 HTTP，不碰 SQLite
  ```bash
  pip install fastmcp httpx
  python mcp_server.py        # stdio；Web 服务没起时每个工具返回带启动提示的 error
  ```
- **换端口**设 `MESSAGE_CLI_URL`（默认 `http://127.0.0.1:8791`），超时 `MESSAGE_CLI_TIMEOUT`（默认 90s）
- **工具清单**：`mail_status / search_mail / read_mail / mail_threads / read_thread / mail_digest / send_mail`
- **安全边界**：只有 `send_mail` 真发信（工具描述要求先向用户确认）；删除/回收站、账号管理、配置写入都**不进** MCP，留在 WebUI

## 验证（2026-07-01 以来，当时的时间窗）

```
Drafts        1 封    正文 1   日期 2026-09-08
INBOX       133 封   正文 133  附件 352  日期 2026-06-30 → 2026-09-09
Sent Items   17 封   正文 17   附件 29   日期 2026-07-08 → 2026-09-09
合计        151 封   正文 151
```

与 IMAP `SINCE 01-Jul-2026` 实测计数（133 / 17 / 1）完全吻合。

## Windows 静默启动与托盘（可选）

`scripts/` 下提供了无窗口启动与托盘管理，不依赖任何第三方模块：

| 脚本 | 作用 |
| --- | --- |
| `start-server.ps1` | 隐藏窗口起服务（日志写 `data/server.log`），等端口就绪后自动开浏览器 |
| `MailTray.ps1` | 托盘图标（WinForms），菜单：打开 / 立即同步 / 重启 / 停止 / 查看日志 / 退出；Global Mutex 保证单例 |
| `MailWebUI.vbs` | 零闪窗拉起托盘（`wscript` 隐藏执行），适合做桌面快捷方式的目标 |
| `MailWebUI.bat` | 同上，直接运行版 |

脚本按以下顺序解析 Python 解释器：`$env:MAILUI_PYTHON` → `scripts\python.local.txt`
→ 项目内 `.venv` → PATH 上的 `python`。

**默认走项目内 `.venv`**：只要按上面「启动」第 1 步建好 `.venv`，托盘与静默启动开箱即用，
不需要任何额外配置。若想临时换解释器（比如另建了别的环境），把路径写进
`scripts\python.local.txt`（参考 `python.local.example.txt`，已 gitignore），或设 `$env:MAILUI_PYTHON`。

> 注意别让脚本落到「PATH 上的 python」：那可能是别的解释器，里面没有
> `fastapi`，托盘会静默启动失败（日志见 `data/server.err.log`）。

## 目录结构

```
message-mail/
├─ run.py                 # CLI：serve / sync / verify / ai
├─ mcp_server.py          # MCP server（7 个邮件工具）
├─ config/
│  ├─ himalaya/
│  │  ├─ config.example.toml    # IMAP/SMTP 配置模板（复制为 config.toml）
│  │  └─ secret.example         # 密码文件模板（复制为 secret）
│  ├─ app.example.toml          # 应用配置模板（AI Key / 同步策略 / 引用头时区）
│  └─ tailnet_apps.json         # tailnet serve 端口登记表
├─ app/                   # 见上文「服务结构」
├─ web/
│  ├─ index.html         # SPA 容器
│  ├─ style.css          # 5 套主题
│  ├─ app.js             # 列表 / 搜索 / 阅读 / 写信 / 账号 / 设置
│  ├─ icon.png           # favicon
│  └─ llms.txt           # 给 AI 看的接口说明
├─ scripts/              # Windows 启动与托盘 + 纯逻辑冒烟测试 + tailnet 助手
├─ assets/               # 应用图标
├─ data/                 # 运行期生成（mail.db / attachments，不入库）
├─ requirements.txt
└─ README.md
```

## 参考项目与致谢

| 项目 | 说明 | 许可 |
| --- | --- | --- |
| [Himalaya](https://github.com/pimalaya/himalaya) | 邮件 CLI，本项目 `config/himalaya/` 的 IMAP 配置格式来源 | MIT OR Apache-2.0 |

## 许可证

本项目采用 [Apache License 2.0](LICENSE) 开源。

```
Copyright 2026 Zhiyuanhe

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```

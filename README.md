# QQ Agently Mail

让你的 AstrBot 机器人用上 QQ 官方 Agent 邮箱：在聊天里收发邮件、搜索邮件、下载附件、开启新邮件实时提醒，还自带管理面板邮箱 WebUI。

> [!TIP]
> 所有登录、登出和会话绑定指令均仅管理员可用。日常收发邮件工具默认只操作当前会话绑定的账号，非管理员无法跨会话访问其他账号。

## 快速开始

1. **安装插件**
   - 方式一：在 AstrBot 插件市场搜索「QQ Agently Mail」，点击安装。
   - 方式二：插件界面右下角加号 → 从链接安装，输入：
     ```
     https://github.com/piexian/astrbot_plugin_qq_agentlymail
     ```
2. **扫码登录并绑定邮箱**
   - 管理员在聊天中发送 `/agently login`，机器人会发出微信登录二维码与备用链接，扫码并在手机上确认即可。
   - 如果该微信下有多个可用的 Agent 邮箱，按提示在 120 秒内回复对应编号即可选择。
   - 登录成功后，当前会话会自动绑定到 `default` 账号；如需登录多个邮箱账号，可指定账号名（例如 `/agently login work`），再在目标群聊或私聊里执行 `/agently bind work`。
3. **试一试**
   - 直接跟机器人说「帮我看看最近有什么未读邮件」，或在 AstrBot 管理面板中打开「邮局」视图扫码登录或查看收件箱。

运行要求：AstrBot `4.16` 及以上（`< 5`），使用 AstrBot 内置依赖，无需额外安装 Python 包。

## 账号与会话绑定

插件支持同时登录多个 QQ Agent 邮箱账号（用自定义账号名区分，缺省为 `default`），并按聊天会话分别绑定。

| 场景 | 普通用户 | 管理员 |
|------|:------:|:------:|
| 在**已绑定**会话中读信、搜索、发信、开关监听 | 可以用（仅限本会话绑定账号） | 可以用（可指定其他已登录账号） |
| 在**未绑定**会话中使用日常邮件工具 | 用不了（无法指定其他账号） | 需显式指定 `account` 参数 |
| `/agently` 全部指令与 `agently_mail_admin` 管理工具 | 用不了 | 可以用 |

关于登录凭证：

- 按账号分别保存在 `data/plugin_data/astrbot_plugin_qq_agentlymail/auth-<账号名>.json`（Linux/macOS 上为 `0600` 权限），到期自动刷新；相当于登录密码，不要发给别人。
- 执行 `/agently logout [账号名]` 会先停止该账号的新邮件监听，再删除本地凭证并解除所有引用该账号的会话绑定。
- 如果凭证失效，重新发送 `/agently login [账号名]` 扫码授权即可覆盖更新。

## 功能一览

AI 在对话中可以自动调用下面 3 个工具，工具启停由 AstrBot 工具管理统一控制。

| 工具 | 支持操作 | 作用 | 使用条件 |
|------|----------|------|----------|
| `agently_mail_read` | `whoami`、`list`、`read`、`search`、`quota` | 查看绑定邮箱与地址、按目录分页列摘要、读全文并可选下载附件、按关键词搜索、估算发信额度 | 所有人¹ |
| `agently_mail_send` | `send`、`reply`、`forward`、`watch_start`、`watch_stop` | 发新邮件（可带本地附件）、回复、转发、启停当前会话的新邮件监听推送 | 所有人¹ |
| `agently_mail_admin` | `login`、`logout`、`status`、`trash`、`empty_trash`、`permanent_delete`、`addresses`、`create_address`、`delete_address` | 扫码登录、登出解绑、查看账号概览、删除邮件与清空垃圾箱、查询／新建／注销邮箱地址 | 仅管理员 |

¹ 日常读写与 `watch_start` 默认使用当前会话绑定账号的主地址，可用 `alias_id` 切换该账号下的其他地址；非管理员不能通过 `account` 参数跨会话访问未绑定到当前会话的账号，仅管理员可用 `account` 指定其他已登录账号。

补充说明：

- **邮箱地址管理**：新建地址由服务端生成前缀，不可自定义；注销地址会删除其下全部邮件且不可恢复，90 天保留期内再新建会自动复用旧地址。
- **附件限制**：发信附件单件与总大小均不超过 20 MB、最多 50 个；通过 `read` 下载的附件会保存到 AstrBot 临时目录并返回路径。

## 指令用法

在聊天里发送 `/agently` 开头的指令，无需经过 AI。以下全部指令均**仅管理员可用**。

### 账号管理

```
/agently login            # 发起微信扫码登录，默认保存为 default 账号并绑定当前会话
/agently login work       # 登录另一个账号并命名为 work
/agently logout           # 删除 default 账号本地凭证、停止监听并解绑关联会话
/agently logout work      # 登出 work 账号
/agently status           # 列出全部已保存凭证的账号，并标注当前会话绑定状态
```

二维码有效期约 2-3 分钟；如果二维码图片无法发送，会自动附带备用浏览器授权链接和验证码。

### 会话绑定

```
/agently bind work        # 把当前会话绑定到已登录的 work 账号
/agently unbind           # 解除当前会话的账号绑定，并停止本会话的新邮件监听
```

切换绑定账号或解绑时，会自动停止当前会话此前运行的旧监听任务。

## 在对话中使用

绑定账号后，直接用自然语言跟机器人交流即可，AI 会在需要时自动调用邮件工具：

- 「看看收件箱里最近 5 封未读邮件」→ 调用 `agently_mail_read`（`list`）
- 「把第二封邮件正文读给我，顺便把附件下下来」→ 调用 `agently_mail_read`（`read`）
- 「搜一下主题里带验证码的邮件」→ 调用 `agently_mail_read`（`search`）
- 「给 alice@example.com 发封邮件，说一下今晚开会时间」→ 调用 `agently_mail_send`（`send`）
- 「帮我开启新邮件提醒」→ 调用 `agently_mail_send`（`watch_start`）

新邮件监听会自动持久化每个会话的账号、地址、事件游标和去重记录：收到新邮件后实时推送到当前会话，重启 AstrBot 后自动恢复监听且尽量避免重复通知；说「关闭新邮件提醒」即可停止并清理监听配置。

## 邮箱 WebUI（邮局）

在 AstrBot 管理面板里直接使用插件自带的网页邮箱：管理面板 → 插件 → 「QQ Agently Mail」→ 视图「邮局」，自动同步 AstrBot WebUI 的语言（简体中文 / English / 日本語 / Русский）与深浅色主题。

| 区域 | 能做什么 |
|------|----------|
| 扫码登录账号 | 直接在页面内获取微信登录二维码，附带可一键复制的原授权链接与验证码（不跳转外链），支持刷新二维码、多邮箱选择与随时中断 |
| 账号与地址切换 | 在多个已登录账号及每个账号的多个邮箱地址间切换，查看各账号凭证状态并一键重新登录失效账号 |
| 目录与服务端搜索 | 浏览收件箱、已发送、垃圾邮件、垃圾箱，支持未读筛选、分页加载与服务端关键词搜索 |
| 邮件阅读与附件 | 查看完整正文（含发件人、抄送等）并直接下载附件到本地浏览器 |
| 写信／回复／转发 | 在线写新邮件（支持多附件上传，总大小 ≤ 20 MB）、回复或转发邮件，发送前带二次确认 |
| 邮件与地址管理 | 把邮件移入垃圾箱；查看别名配额上限，在线新建或注销邮箱地址 |
| 额度面板 | 查看发信限额、今日已发计数与附件大小限制 |

要注意：

- 需要先登录 AstrBot 管理面板才能访问，鉴权由 Dashboard 统一负责。
- WebUI 发信同样受 `mail_send_whitelist` 白名单限制，不会绕过配置约束。

## 配置说明

在 AstrBot 管理面板的插件配置里修改。

| 配置项 | 类型 | 默认值 | 什么时候需要改 |
|--------|------|--------|----------------|
| `mail_send_whitelist` | 列表 | 空（不限制） | 希望限制机器人只能往指定邮箱发信时填写允许的收件人地址；对 LLM 工具与 WebUI 的发信、回复、转发同时生效（含抄送、密送与回复原信推导的收件人） |

## 安全说明

- 全部 `/agently` 管理指令与 `agently_mail_admin` 工具（含登录、登出、删除邮件、注销地址）仅管理员可用。
- 非管理员只能操作当前会话已绑定的账号，无法通过自然语言诱导 AI 传 `account` 参数去读写或监听未绑定到当前会话的其他账号。
- 邮件正文返回给 AI 时采用围栏包裹，发件人、主题、附件名等元数据做单行清洗，降低恶意邮件内容触发提示词注入的风险。
- 发信操作受 `mail_send_whitelist` 后端强制校验，且采用服务端两阶段确认机制；LLM 发信的本地附件路径仅允许读取 AstrBot 临时目录内的普通文件（解析符号链接防越界），不会读取插件凭证或服务器其他目录。
- 本地凭证文件以 `0600` 权限按账号隔离存放，插件日志与 Web API 响应均不输出 Token、确认令牌或上游附件直链。
- LLM 工具与 WebUI 下载的附件统一暂存到 AstrBot 临时目录（`data/temp/astrbot_plugin_qq_agentlymail/`），交由 AstrBot 统一管理。

## 项目结构

```
astrbot_plugin_qq_agentlymail/
├── main.py                         # 插件入口：指令组、工具注册、监听持久化与生命周期
├── metadata.yaml                   # 插件元信息与视图声明
├── _conf_schema.json               # 插件配置项定义
├── README.md                       # 使用说明
├── CHANGELOG.md                    # 更新日志
├── LICENSE                         # AGPL-3.0 许可协议
├── .astrbot-plugin/i18n/
│   └── zh-CN.json                  # 视图中文名称
├── docs/
│   └── web-api.md                  # WebUI 后端 API 契约文档
├── agentlymail/                    # 核心模块
│   ├── client.py                   # 邮箱 HTTP 客户端、鉴权刷新与接口封装
│   ├── constants.py                # 接口地址与常量定义
│   ├── headless.py                 # 微信扫码登录流程与多邮箱选择
│   ├── models.py                   # 邮件、别名、分页与限额数据模型
│   ├── oauth.py                    # OAuth 设备流授权与 Token 刷新
│   ├── render.py                   # 邮件摘要、正文围栏与新邮件提醒格式化
│   ├── session.py                  # 登录防重入登记
│   ├── token_store.py              # 多账号凭证原子读写（0600 权限）
│   ├── tools.py                    # 3 个 LLM FunctionTool 与权限校验
│   ├── watcher.py                  # 新邮件长轮询监听与游标去重
│   └── web_api.py                  # WebUI 后端路由实现
├── web/                            # WebUI 前端源码（Vue 3 + TypeScript + Vite）
└── views/mail/                     # WebUI 前端构建产物
```

## 更新日志

详见 [CHANGELOG.md](./CHANGELOG.md)。

## 相关链接

- [AstrBot 文档](https://docs.astrbot.app/)
- [QQ Agent 邮箱](https://agent.qq.com/)
- [Web API 契约说明](docs/web-api.md)
- [Issues](https://github.com/piexian/astrbot_plugin_qq_agentlymail/issues)

## 许可

AGPL-3.0 License

<div align="center">

**如果这个插件对你有帮助，请给个 ⭐ Star 支持一下！**

</div>

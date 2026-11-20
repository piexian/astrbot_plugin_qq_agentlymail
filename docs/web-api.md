# WebUI 真实 Web API 契约（P5.3/P5.4）

后端实现于 `agentlymail/web_api.py`，在 `main.py` 的 `initialize()` 中经 `register_web_apis` 注册，供 `window.AstrBotPluginView` bridge 调用。所有路由带前缀 `/astrbot_plugin_qq_agentlymail`，bridge endpoint 用去掉前缀的相对路径。鉴权由 Dashboard 转发层完成，插件不再重复鉴权。

## 调用约定

- 读操作用 `bridge.apiGet(endpoint, params)`，写操作用 `bridge.apiPost(endpoint, body)`（bridge 无 DELETE，写操作一律 POST）。
- 成功响应是业务 JSON 本身，不包 `{status:"ok"}` 信封。
- 失败响应为 `{status:"error", message, data}` 加非 2xx 状态码，bridge 以 `Error(message)` reject。`data.code` 仅透传上游枚举错误码（如 `VALIDATION_ERROR`）。
- 任何响应都不含 `access_token`、`refresh_token`、`confirmation_token`、`download_url` 或凭证文件路径。

## 路由表

| 方法 | endpoint | 参数 | 响应要点 |
|---|---|---|---|
| GET | `accounts` | 无 | `{accounts:[{account,status,app_id,expires_at,email,aliases,error}]}`；status 为 ok/invalid_credentials/auth_expired/error，单账号失败隔离 |
| GET | `me` | `account` | `{account,aliases,rate_limits,constraints,scopes,credential:{app_id,expires_at,expired},sent_today_local,account_info}`；account_info 失败降级 null |
| GET | `messages` | `account,alias_id,dir?,limit?,cursor?,unread?` | `{data:[摘要],has_more,next_cursor}`；dir 默认 inbox，limit 默认 20（1–50），unread 仅 true 生效 |
| GET | `search` | `account,alias_id,q,search_in?,limit?,cursor?` | 同 messages 形状；服务端真实搜索，search_in 默认 SEARCH_IN_ALL |
| GET | `message` | `account,alias_id,message_id` | 全文 `{message_id,subject,body,body_format,from_,to,cc,bcc,attachments,created_at}`；attachments 只含 attachment_id/filename/content_type/size |
| GET | `attachment` | `account,alias_id,message_id,attachment_id 或 index` | 二进制下载（Content-Disposition）；后端重读邮件定位，只信任上游 download_url |
| POST | `trash` | `{account,alias_id,message_id}` | `{message_id,trashed:true}`（软删除） |
| POST | `send` | `{account,alias_id,to,cc?,bcc?,subject,body,body_format,attachments?}` | `{queued:true,to,subject,attachment_count,local_count_recorded}` |
| POST | `reply` | `{account,alias_id,message_id,body,body_format,reply_all?}` | `{queued:true,message_id,reply_all,local_count_recorded}` |
| POST | `forward` | `{account,alias_id,message_id,to,body,body_format,include_attachments?}` | `{queued:true,message_id,to,include_attachments,local_count_recorded}`；include_attachments 默认 true |
| POST | `create_address` | `{account}` | `{alias:{alias_id,email,is_primary},reused,aliases,refresh_required,refresh_error}`；刷新失败时 aliases=null、refresh_required=true，alias 取写结果字段 |
| POST | `delete_address` | `{account,alias_id}` | `{alias_id,aliases,refresh_required,refresh_error}`；刷新失败时 aliases=null、refresh_required=true；允许注销到零地址 |

摘要项只含 `message_id,subject,from_,created_at,is_read`，不含 body/preview/attachments；正文与附件须选中后另调 `message`。协议无独立标记已读端点，`is_read` 以重新列表为准。

## 附件方案

- 发送：浏览器把 File 读成纯 Base64（不带 data URL 前缀），随 send body 内联提交 `attachments:[{filename,content_type?,content}]`。后端解码校验后组装协议五字段，`size`/`sha1` 由后端计算，复用浏览器 Base64 字符串。单件与总量均 ≤20MB、≤50 件；所有 POST 原始 body ≤28MiB（超限 413）。不走 upload 端点、不用 file_id。
- 回复无附件；转发用 `include_attachments:true` 由服务端携带原附件。
- 下载：`bridge.download("attachment", {account,alias_id,message_id,attachment_id 或 index}, filename)`。后端重读邮件定位附件，大附件用上游 download_url（不带 Bearer），落盘到 `<AstrBot data>/temp/astrbot_plugin_qq_agentlymail/web-downloads/<uuid>/<清洗文件名>` 后经 file_response 返回；临时文件交由 AstrBot 临时目录统一管理，插件不再执行过期扫描或卸载清理。

## 错误码

| HTTP | 含义 |
|---|---|
| 400 | 参数/字段校验失败、上游 400/422 |
| 401 | 凭证失效或 OAuth 错误，提示聊天执行 `/agently login` |
| 403 | 收件人被 `mail_send_whitelist` 拦截，或上游 403 |
| 404 | 账号不存在、alias 不归属、邮件/附件不存在 |
| 413 | 请求体超过 28MiB |
| 502 | 上游 ≥500 或传输错误 |
| 504 | 上游超时。读操作提示稍后重试；**写操作提示“操作结果未知，请检查已发送/邮箱状态后再试”**（第二阶段确认请求可能已到达服务端） |
| 500 | 凭证存储错误或未预期异常，详情见插件日志 |

写成功后的本地计数失败与 create/delete 后的身份刷新失败不再返回错误响应：前者仍返回 `queued:true` 且 `local_count_recorded=false`，只记插件日志；后者仍返回写结果且 `aliases=null`、`refresh_required=true`，`refresh_error` 给安全文案（成功为 null）。

发件白名单与 LLM 工具同路径：send 校验 to+cc+bcc，forward 校验 to，reply 配置白名单时先读原信推导收件人再校验，WebUI 无法绕过。

## 前端契约差异（替换 mock 时注意）

1. `listMessages` 全量带正文 → 按 (account,alias_id,dir) 分页摘要，选中后另调 `message`。
2. 摘要无附件字段、协议无附件筛选参数，“有附件”筛选应禁用或隐藏，不为筛选逐封读全文。
3. 本地关键词过滤 → 调 `search`；未读筛选 → `messages?unread=true`。
4. `markRead` 无对应端点；读信即 GET `message`。
5. `send` 返回 `{queued:true}` 而非邮件对象；to/cc 改字符串数组；发送后切到 sent 重新列表。reply/forward 改调专属端点。`local_count_recorded=false` 仅表示本地计数未记录，仍按发送成功处理。
6. 地址配额用 `me.account_info.agent_alias_limit`，不再硬编码 5；create/delete 响应自带刷新后 aliases，刷新失败时 `aliases=null` 且 `refresh_required=true`，前端应重新拉取 accounts。
7. MailAccount 的 name/initials/tone 是展示装饰，由前端从 account/email 派生。
8. 添加账号无端点，继续提示聊天 `/agently login`；本阶段不含扫码登录与 SSE 推送。
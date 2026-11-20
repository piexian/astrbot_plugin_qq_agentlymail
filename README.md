# QQ Agently Mail

QQ Agently Mail 插件直连 `agent.qq.com`，提供 LLM 工具与 WebUI，目前仍在开发中。

## 使用

- `/agently login [账号名]`：管理员发起微信扫码登录（账号名缺省 `default`），二维码 2-3 分钟过期，成功后自动绑定当前会话。
- `/agently logout [账号名]`：管理员删除该账号本地凭证并解绑引用它的会话。
- `/agently status`：管理员列出全部账号并标注当前会话绑定。
- `/agently bind <账号名>` / `/agently unbind`：管理员绑定或解除当前会话的账号。

## WebUI

插件视图 `mail` 通过 AstrBot Web API 提供真实多账号邮箱管理，支持读信、搜索、发送、附件和地址管理。前端源码位于 `web/`，构建产物位于 `views/mail/`。

## LLM 工具

会话绑定账号后，模型可用三个工具操作邮箱，输出统一为 JSON 文本，正文围栏包裹防注入：

- `agently_mail_read`（所有用户）：`whoami` 绑定信息、`list` 分页列摘要、`read` 读全文（可下载附件到 AstrBot 临时目录）、`search` 搜索、`quota` 额度估算。
- `agently_mail_send`（所有用户）：`send`/`reply`/`forward` 发信（两阶段确认、本地附件、白名单校验），`watch_start`/`watch_stop` 启停新邮件监听，新邮件自动推送到会话。
- `agently_mail_admin`（仅管理员）：`login` 扫码登录（二维码发到会话）、`logout` 删凭证解绑、`status` 账号概览、`trash`/`empty_trash`/`permanent_delete` 删除类操作。

日常读写和 `watch_start` 可用可选参数 `account`、`alias_id` 指定已登录账号及其所属地址；缺省使用会话绑定账号的主地址。

新邮件监听按会话保存账号、地址、事件游标和去重记录，插件重启后自动恢复；`watch_stop` 同时停止任务并清理配置。

配置项：`mail_send_whitelist`（收件人白名单，留空不限）。LLM 工具由 AstrBot 工具管理统一启停。
凭证保存于 `data/plugin_data/astrbot_plugin_qq_agentlymail/`（0600 权限，按账号一个文件）。

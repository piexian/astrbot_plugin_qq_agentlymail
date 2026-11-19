# QQ Agently Mail

QQ Agently Mail 插件直连 `agent.qq.com`，提供 LLM 工具与 WebUI，目前仍在开发中。

## 使用

- `/agently login [账号名]`：管理员发起微信扫码登录（账号名缺省 `default`），二维码 2-3 分钟过期，成功后自动绑定当前会话。
- `/agently logout [账号名]`：管理员删除该账号本地凭证并解绑引用它的会话。
- `/agently status`：管理员列出全部账号并标注当前会话绑定。
- `/agently bind <账号名>` / `/agently unbind`：绑定或解除当前会话的账号。

凭证保存于 `data/plugin_data/astrbot_plugin_qq_agentlymail/`（0600 权限，按账号一个文件）。

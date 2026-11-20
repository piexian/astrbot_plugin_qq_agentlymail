"""把协议模型渲染为可读文本，正文一律围栏隔离防注入。"""

from __future__ import annotations

from .models import Message, MessageSummary, Page


def fence_body(body: str) -> str:
    """把不可信正文包进代码围栏，围栏长度压过正文内最长的反引号串。"""
    longest = 0
    run = 0
    for char in body:
        if char == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    marker = "`" * max(3, longest + 1)
    return f"{marker}\n{body}\n{marker}"


def _sender_text(sender) -> str:
    if sender is None:
        return "（无）"
    if sender.name:
        return f"{sender.name} <{sender.email}>"
    return sender.email


def _read_flag(summary: MessageSummary) -> str:
    if summary.is_read is True:
        return "｜已读"
    if summary.is_read is False:
        return "｜未读"
    return "｜已读状态未知"


def format_summary_list(page: Page[MessageSummary]) -> str:
    """把一页邮件摘要渲染成每封一行的清单文本。"""
    lines = []
    for item in page.data:
        sender = _sender_text(item.from_)
        subject = item.subject or "（无主题）"
        created = item.created_at or "时间未知"
        lines.append(
            f"- {item.message_id}｜发件人 {sender}｜主题 {subject}"
            f"｜{created}{_read_flag(item)}"
        )
    if not lines:
        return "（没有邮件）"
    return "\n".join(lines)


def format_message(message: Message, max_chars: int) -> str:
    """渲染一封完整邮件，正文截断到 max_chars 后围栏包裹。"""
    body = message.body
    truncated = len(body) > max_chars
    if truncated:
        body = body[:max_chars]
    lines = [
        f"message_id：{message.message_id}",
        f"主题：{message.subject or '（无主题）'}",
        f"发件人：{_sender_text(message.from_)}",
        f"收件人：{', '.join(_sender_text(s) for s in message.to) or '（无）'}",
        f"抄送：{', '.join(_sender_text(s) for s in message.cc) or '（无）'}",
        f"时间：{message.created_at or '未知'}",
        f"正文格式：{message.body_format}",
    ]
    if truncated:
        lines.append(f"（正文超过 {max_chars} 字符，已截断）")
    lines.append(fence_body(body))
    if message.attachments:
        lines.append("附件：")
        for attachment in message.attachments:
            attachment_id = attachment.attachment_id or "（无 attachment_id）"
            filename = attachment.filename or "（无文件名）"
            content_type = attachment.content_type or "未知类型"
            size = attachment.size if attachment.size is not None else "未知"
            lines.append(
                f"- {attachment_id}｜{filename}｜{content_type}｜{size} 字节"
            )
    return "\n".join(lines)


def format_new_mail_notice(message: Message) -> str:
    """新邮件推送到会话的简版通知文本。"""
    sender = _sender_text(message.from_)
    subject = message.subject or "（无主题）"
    created = message.created_at or "未知时间"
    return (
        f"收到新邮件：发件人 {sender}，主题 {subject}，时间 {created}。\n"
        f"message_id：{message.message_id}\n"
        "可让我读取这封邮件查看全文。"
    )

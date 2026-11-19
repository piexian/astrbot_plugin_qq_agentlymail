"""从设备流授权页提取微信扫码二维码图片。"""

from __future__ import annotations

import re

import httpx

# 授权页通过跨域 iframe 嵌入 open.weixin.qq.com 的二维码直链（JPEG 470x470）。
_QRCODE_URL_PATTERN = re.compile(
    r"https?://open\.weixin\.qq\.com/connect/qrcode/[^\"'\s<>\\]+",
    re.IGNORECASE,
)


async def fetch_wechat_qr_image(
    http: httpx.AsyncClient,
    browser_url: str,
) -> bytes | None:
    """抓取授权页里的微信二维码图片字节，任一步失败返回 None。"""
    try:
        page = await http.get(browser_url)
        page.raise_for_status()
    except httpx.HTTPError:
        return None
    match = _QRCODE_URL_PATTERN.search(page.text)
    if match is None:
        return None
    try:
        image = await http.get(match.group(0))
        image.raise_for_status()
    except httpx.HTTPError:
        return None
    if not image.content:
        return None
    return image.content

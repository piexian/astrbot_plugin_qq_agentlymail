"""定义 QQ Agently Mail 协议使用的固定地址和客户端标识。"""

import platform
import sys

API_BASE = "https://api.agent.qq.com"
AUTH_BASE = "https://auth.agent.qq.com"
CLI_VERSION = "1.0.18"

_GOOS_BY_SYSPLATFORM = {"win32": "windows", "cygwin": "windows", "msys": "windows"}
_GOARCH_BY_MACHINE = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "i386": "386",
    "i686": "386",
    "x86": "386",
}

_GOOS = _GOOS_BY_SYSPLATFORM.get(sys.platform, sys.platform)
_MACHINE = platform.machine().lower()
_GOARCH = _GOARCH_BY_MACHINE.get(_MACHINE, _MACHINE or "unknown")

# 与官方 agently-cli 的 buildinfo.UserAgent 格式一致：agently-cli/{版本} ({GOOS}/{GOARCH})。
USER_AGENT = f"agently-cli/{CLI_VERSION} ({_GOOS}/{_GOARCH})"
DEVICE_PATH = "/oauth/device?func=1"
TOKEN_PATH = "/oauth/token"

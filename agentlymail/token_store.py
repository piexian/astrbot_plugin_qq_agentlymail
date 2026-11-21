"""实现多账号凭证的安全存储和原子轮换。"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import AsyncIterator, Any

from astrbot.api import logger

try:
    from astrbot.api.util import get_astrbot_data_path
except ImportError:  # 兼容旧版 AstrBot：公共 API 未导出时回退到内部路径助手。
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path


class TokenStoreError(RuntimeError):
    """表示凭证存储或解析失败。"""


@dataclass(slots=True)
class TokenBundle:
    """保存一个账号的 OAuth 凭证和过期时间。"""

    app_id: str | None
    access_token: str
    refresh_token: str | None
    expires_at: float

    @classmethod
    def from_dict(cls, value: Any) -> TokenBundle:
        if not isinstance(value, dict):
            raise TokenStoreError("凭证文件顶层必须是 JSON 对象")
        app_id = value.get("app_id")
        access_token = value.get("access_token")
        refresh_token = value.get("refresh_token")
        expires_at = value.get("expires_at")
        if app_id is not None and (not isinstance(app_id, str) or not app_id):
            raise TokenStoreError("凭证 app_id 必须是非空字符串或 null")
        if not isinstance(access_token, str) or not access_token:
            raise TokenStoreError("凭证 access_token 必须是非空字符串")
        if refresh_token is not None and (
            not isinstance(refresh_token, str) or not refresh_token
        ):
            raise TokenStoreError("凭证 refresh_token 必须是非空字符串或 null")
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
            raise TokenStoreError("凭证 expires_at 必须是数字")
        return cls(
            app_id=app_id,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=float(expires_at),
        )


class TokenStore:
    """按账号安全读写 OAuth 凭证文件。"""

    def __init__(self, root_dir: str | os.PathLike[str] | None = None):
        self.name = Path(__file__).resolve().parent.parent.name
        if root_dir is None:
            root_dir = Path(get_astrbot_data_path()) / "plugin_data" / self.name
        self.root_dir = Path(root_dir)
        self._locks: dict[str, asyncio.Lock] = {}

    def path_for(self, account: str) -> Path:
        """返回账号对应的凭证路径并拒绝路径穿越。"""
        if not account or account in {".", ".."}:
            raise TokenStoreError("账号名不能为空或为路径特殊值")
        if "\x00" in account or "/" in account or "\\" in account:
            raise TokenStoreError(f"账号名包含非法路径字符: {account!r}")
        return self.root_dir / f"auth-{account}.json"

    def _lock_for(self, account: str) -> asyncio.Lock:
        self.path_for(account)
        lock = self._locks.get(account)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[account] = lock
        return lock

    @asynccontextmanager
    async def locked(self, account: str) -> AsyncIterator[None]:
        """锁定账号以便完成一次读取、刷新和落盘轮换。"""
        logger.debug(f"[agently] 凭证存储 等待账号锁 账号={account}")
        async with self._lock_for(account):
            logger.debug(f"[agently] 凭证存储 已获取账号锁 账号={account}")
            try:
                yield
            finally:
                logger.debug(f"[agently] 凭证存储 释放账号锁 账号={account}")

    async def load(self, account: str) -> TokenBundle | None:
        """读取账号凭证，不存在时返回 None。"""
        async with self.locked(account):
            return await self._load_unlocked(account)

    async def save(self, account: str, bundle: TokenBundle) -> None:
        """以临时文件替换方式保存账号凭证。"""
        async with self.locked(account):
            await self._save_unlocked(account, bundle)

    async def delete(self, account: str) -> bool:
        """删除账号凭证文件并确认已不存在，未删除时返回 False。"""
        logger.debug(f"[agently] 凭证存储 删除开始 账号={account}")
        async with self.locked(account):
            path = self.path_for(account)
            try:
                await asyncio.to_thread(path.unlink)
            except FileNotFoundError:
                logger.debug(f"[agently] 凭证存储 删除跳过，文件不存在 路径={path}")
                return False
            except OSError as exc:
                logger.warning(
                    f"[agently] 凭证存储 删除失败 账号={account} 路径={path} "
                    f"异常={type(exc).__name__}"
                )
                raise TokenStoreError(f"删除账号 {account!r} 凭证失败: {path}") from exc
            deleted = not path.exists()
            if deleted:
                logger.info(f"[agently] 凭证存储 凭证已删除 账号={account} 路径={path}")
            else:
                logger.warning(f"[agently] 凭证存储 删除后文件仍存在 账号={account} 路径={path}")
            return deleted

    async def list_accounts(self) -> list[str]:
        """枚举已保存凭证的账号名。"""
        logger.debug(f"[agently] 凭证存储 扫描开始 根目录={self.root_dir}")

        def scan() -> list[str]:
            try:
                names = [
                    path.stem.removeprefix("auth-")
                    for path in self.root_dir.glob("auth-*.json")
                ]
            except OSError as exc:
                logger.warning(
                    f"[agently] 凭证存储 扫描失败 根目录={self.root_dir} "
                    f"异常={type(exc).__name__}"
                )
                raise TokenStoreError(f"扫描凭证目录失败: {self.root_dir}") from exc
            return sorted(names)

        names = await asyncio.to_thread(scan)
        logger.debug(
            f"[agently] 凭证存储 扫描完成 根目录={self.root_dir} 数量={len(names)}"
        )
        return names

    async def _load_unlocked(self, account: str) -> TokenBundle | None:
        path = self.path_for(account)
        try:
            content = await asyncio.to_thread(path.read_text, encoding="utf-8")
        except FileNotFoundError:
            logger.debug(f"[agently] 凭证存储 读取未命中 账号={account} 路径={path}")
            return None
        except OSError as exc:
            logger.warning(
                f"[agently] 凭证存储 读取失败 账号={account} 路径={path} "
                f"异常={type(exc).__name__}"
            )
            raise TokenStoreError(f"读取账号 {account!r} 凭证失败: {path}") from exc
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            logger.warning(
                f"[agently] 凭证存储 JSON 解析失败 账号={account} 路径={path} "
                f"行={exc.lineno} 列={exc.colno}"
            )
            raise TokenStoreError(f"解析账号 {account!r} 凭证失败: {path}") from exc
        try:
            bundle = TokenBundle.from_dict(value)
        except TokenStoreError as exc:
            logger.warning(
                f"[agently] 凭证存储 凭证结构校验失败 账号={account} 路径={path}"
            )
            raise TokenStoreError(f"校验账号 {account!r} 凭证失败: {path}: {exc}") from exc
        logger.debug(
            f"[agently] 凭证存储 读取命中 账号={account} 路径={path} "
            f"过期时间={bundle.expires_at:.3f}"
        )
        return bundle

    async def _save_unlocked(self, account: str, bundle: TokenBundle) -> None:
        path = self.path_for(account)
        logger.debug(
            f"[agently] 凭证存储 保存开始 账号={account} 路径={path} "
            f"过期时间={bundle.expires_at:.3f}"
        )
        try:
            await asyncio.to_thread(self._write_atomic, path, bundle)
        except TokenStoreError:
            raise
        except OSError as exc:
            logger.warning(
                f"[agently] 凭证存储 写入失败 账号={account} 路径={path} "
                f"异常={type(exc).__name__}"
            )
            raise TokenStoreError(f"写入账号 {account!r} 凭证失败: {path}") from exc
        logger.debug(f"[agently] 凭证存储 保存完成 账号={account} 路径={path}")

    @staticmethod
    def _write_atomic(path: Path, bundle: TokenBundle) -> None:
        logger.debug(f"[agently] 凭证存储 原子写入开始 路径={path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            asdict(bundle),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            text=True,
        )
        temporary_path = Path(temporary_name)
        logger.debug(f"[agently] 凭证存储 临时文件已创建 路径={temporary_path}")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        except OSError as exc:
            logger.warning(
                f"[agently] 凭证存储 原子写入失败 路径={path} 异常={type(exc).__name__}"
            )
            raise TokenStoreError(f"原子替换凭证文件失败: {path}") from exc
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning(
                    f"[agently] 凭证存储 临时文件清理失败 路径={temporary_path} "
                    f"异常={type(exc).__name__}"
                )
        logger.debug(f"[agently] 凭证存储 原子替换完成 路径={path}")

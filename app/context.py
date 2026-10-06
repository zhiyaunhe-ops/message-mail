"""统一运行上下文 —— 拆成几个内聚部件，而不是一个什么都管的上帝对象。

这个进程里跑着邮件模块，它共享下面三块东西：

    Cache        TTL 缓存（带 key 前缀，可按模块精确失效）
    SyncState    同步进度、最近一次同步结果、运行日志
    AppContext   资源工厂：账号 / SQLite Store / IMAP（按需创建、用后即关）
                 + 写操作串行锁（同一时刻只允许一个 IMAP 写动作）

路由层通过 app/deps.py 的 Depends 拿到需要的部件；CLI / 服务层可以直接用 ctx。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from typing import Iterator

from . import appstate
from . import settings
from .imap_client import IMAPClient
from .settings import Account, load_account
from .store import Store

LOG_LIMIT = 300     # 运行日志最多保留多少条
CACHE_TTL = 300     # 默认缓存 5 分钟
CACHE_MAX = 256     # 缓存条目上限（超了淘汰最旧的）


# ------------------------------------------------------------------ 缓存
class TTLCache:
    """带过期时间的小缓存；按插入顺序淘汰，避免无上限增长。"""

    def __init__(self, ttl: float = CACHE_TTL, maxsize: int = CACHE_MAX) -> None:
        self._lock = threading.RLock()
        self._data: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
        self.ttl = ttl
        self.maxsize = maxsize

    def get(self, key: str) -> Any | None:
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            if item[0] <= time.time():
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return item[1]

    def put(self, key: str, value: Any, ttl: float | None = None) -> Any:
        with self._lock:
            self._data[key] = (time.time() + (self.ttl if ttl is None else ttl), value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)
            return value

    def invalidate(self, prefix: str | None = None) -> int:
        with self._lock:
            if prefix is None:
                n = len(self._data)
                self._data.clear()
                return n
            keys = [k for k in self._data if k.startswith(prefix)]
            for k in keys:
                self._data.pop(k, None)
            return len(keys)

    def keys(self) -> list[str]:
        with self._lock:
            return sorted(self._data)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


# ------------------------------------------------------------------ 同步状态
class SyncState:
    """同步进度 + 运行日志（只负责状态，不碰 IO）。"""

    def __init__(self, log_limit: int = LOG_LIMIT) -> None:
        self._lock = threading.RLock()
        self.syncing: bool = False
        self.last_sync: dict | None = None
        self._log: list[str] = []
        self.log_limit = log_limit

    # ---------------- 同步旗（手动 + 后台自动同步共用） ----------------
    def try_begin(self) -> bool:
        """抢同步旗：抢到返回 True，别人正在同步则返回 False。

        手动 /api/sync 与后台自动同步都走这里 —— 两台 IMAP 会话同时跑没有意义，
        而且会互相拖慢；先到先得，晚到的直接跳过（不是错误）。
        """
        with self._lock:
            if self.syncing:
                return False
            self.syncing = True
            return True

    def end(self) -> None:
        with self._lock:
            self.syncing = False

    def log_add(self, message: str) -> None:
        with self._lock:
            self._log.append(message)
            if len(self._log) > self.log_limit:
                del self._log[: len(self._log) - self.log_limit]

    def recent_log(self, n: int = 50) -> list[str]:
        with self._lock:
            return self._log[-n:]

    def clear_log(self) -> None:
        with self._lock:
            self._log.clear()


# ------------------------------------------------------------------ 资源工厂
class AppContext:
    """共享资源（账号 / 索引 / 邮件连接）+ 写锁。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._account: Account | None = None
        self._account_name: str = ""      # 运行时选中的账号；空 = 用配置里的 default
        self._store: Store | None = None

        self.cache = TTLCache()
        self.sync = SyncState()
        # IMAP 写操作（同步 / 发信 / 删除 / 标已读）串行化：一台服务器跑一个即可
        self.write_lock = threading.RLock()

    # ---------------- 账号 / 索引 ----------------
    def account_name(self) -> str:
        """当前生效的账号名：运行时选中的 > state.json 记着的 > 配置 default > 第一个。"""
        with self._lock:
            names = settings.accounts()
            if self._account_name and self._account_name in names:
                return self._account_name
            saved = appstate.get("active_account")
            if saved and saved in names:
                self._account_name = saved
                return saved
            self._account_name = settings.default_account_name()
            return self._account_name

    def account(self) -> Account:
        with self._lock:
            if self._account is None:
                self._account = load_account(self.account_name())
            return self._account

    def store(self) -> Store:
        with self._lock:
            if self._store is None:
                self._store = Store(settings.db_path_for(self.account_name()))
            return self._store

    def _drop_account_locked(self) -> None:
        """丢掉当前账号的缓存与连接（切换 / 配置变更时用）。"""
        if self._store is not None:
            self._store.close()
            self._store = None
        self._account = None

    def switch_account(self, name: str) -> dict:
        """切换到另一个邮箱账号：换库、清缓存，下一次请求就是新账号的世界。"""
        with self._lock:
            if name not in settings.accounts():
                raise KeyError(name)
            self._drop_account_locked()
            self._account_name = name
            appstate.set("active_account", name)
            self.cache.invalidate()      # 目录清单 / 联系人 / 模块自检都跟着账号走
            self.sync.last_sync = None
            acc = self.account()
            self.sync.log_add(f"切换邮箱 -> {name} <{acc.email}>")
            return {"name": name, "email": acc.email, "display_name": acc.display_name}

    def reload_accounts(self) -> None:
        """账号被增删改后调用：丢掉缓存，让下一次请求重新读配置。"""
        with self._lock:
            self._drop_account_locked()
            settings.invalidate_config()
            self.cache.invalidate()

    @property
    def own_emails(self) -> set[str]:
        email = (self.account().email or "").strip().lower()
        return {email} if email else set()

    def is_mine(self, msg: dict) -> bool:
        addr = ((msg.get("from") or {}).get("email") or "").strip().lower()
        if addr and addr in self.own_emails:
            return True
        return msg.get("folder") in ("Sent Items", "Drafts", "Sent", "已发送")

    # ---------------- IMAP：按需创建、用后即关 ----------------
    @contextmanager
    def imap(self, timeout: int = 60) -> Iterator[IMAPClient]:
        """每段逻辑用独立连接，出了 with 就关，避免共享有状态连接。"""
        c = IMAPClient(self.account(), timeout=timeout)
        try:
            c.connect()
            yield c
        finally:
            c.close()

    @contextmanager
    def imap_write(self) -> Iterator[IMAPClient]:
        """写操作：独立连接 + 全局互斥（同一时刻只跑一个写动作）。"""
        with self.write_lock, self.imap() as c:
            yield c

    # ---------------- 生命周期 ----------------
    def close(self) -> None:
        with self._lock:
            if self._store is not None:
                self._store.close()
                self._store = None
            self._account = None
            self.cache.invalidate()
            self.sync.clear_log()


ctx = AppContext()


# ------------------------------------------------------------------ 便捷函数
def account() -> Account:
    return ctx.account()


def account_name() -> str:
    return ctx.account_name()


def store() -> Store:
    return ctx.store()


def imap() -> Any:
    return ctx.imap()


def imap_write() -> Any:
    return ctx.imap_write()


def own_emails() -> set[str]:
    return ctx.own_emails


def is_mine(msg: dict) -> bool:
    return ctx.is_mine(msg)


def log_add(message: str) -> None:
    ctx.sync.log_add(message)

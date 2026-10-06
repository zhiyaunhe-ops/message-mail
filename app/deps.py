"""FastAPI 依赖注入：路由通过 Depends 拿上下文部件，而不是 import 模块级单例。

好处：
  · 生命周期由框架管（app.state.ctx 在 lifespan 里就绪、退出时释放）
  · 路由签名自解释（要 Store 就写 s: Store = Depends(get_store)）
  · 测试时可替换（override 依赖即可）
"""
from __future__ import annotations

from fastapi import Depends, Request

from .context import AppContext, SyncState, TTLCache
from .settings import Account
from .store import Store


def get_ctx(request: Request) -> AppContext:
    """当前服务的运行上下文（lifespan 里挂到 app.state）。"""
    return request.app.state.ctx


def get_cache(ctx: AppContext = Depends(get_ctx)) -> TTLCache:
    return ctx.cache


def get_sync(ctx: AppContext = Depends(get_ctx)) -> SyncState:
    return ctx.sync


def get_store(ctx: AppContext = Depends(get_ctx)) -> Store:
    return ctx.store()


def get_account(ctx: AppContext = Depends(get_ctx)) -> Account:
    return ctx.account()

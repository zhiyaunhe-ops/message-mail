"""Message WebUI 服务（邮件）。

给人类：5 套美学主题的 WebUI（/）
给 AI  ：结构化 JSON 接口（/api/...），说明见 /llms.txt 与 /api/ai/schema

本文件只做装配：
    app 实例 / CORS / 静态资源 / 首页 / llms.txt
    include_router(api_mail)           —— 邮件、会话、分类、AI 端点
    include_router(api_accounts)       —— 多邮箱账号管理
    include_router(api_ai)             —— AI 写正文 + 同步/展示设置
    生命周期（启动补分类与会话归组、启动后台同步、退出关闭连接）
共享状态统一在 app/context.py。
"""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import autosync
from .api_accounts import router as accounts_router
from .api_ai import router as ai_router
from .api_mail import remote_folders
from .api_mail import router as mail_router
from .appconfig import ensure_example
from .context import ctx
from .services import reindex
from .settings import ROOT

WEB_DIR = ROOT / "web"


def _warmup() -> None:
    """后台预热：重建派生索引（分类 / 会话归组）+ 预取目录清单。

    跑在独立线程里 —— uvicorn 一就绪就能响应请求，不用等这些做完，
    重启后的第一次访问也就不再被拖住。
    """
    try:
        ensure_example()          # 首次运行生成 config/app.example.toml
    except Exception:
        pass
    try:
        reindex(classify_all=False, rebuild=True)
    except Exception as e:
        ctx.sync.log_add(f"startup reindex failed: {e}")
    try:
        remote_folders(refresh=True)   # 顺手把目录清单缓存填好
    except Exception:
        pass
    # 后台同步线程：先按设定补一次「近 N 天」（默认近 1 周），之后每 interval 分钟一轮。
    # 放在最后启动 —— 它会自己开线程，不必占着预热线程等；设定在 config/app.toml [sync]。
    try:
        autosync.start()
    except Exception as e:
        ctx.sync.log_add(f"autosync start failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动：把统一上下文挂到 app.state（供 Depends 注入），后台线程做预热；
    退出：关闭 SQLite 连接、清缓存。"""
    app.state.ctx = ctx
    threading.Thread(target=_warmup, name="warmup", daemon=True).start()
    yield
    ctx.close()


app = FastAPI(
    title="Message WebUI / Mail API",
    version="1.1.0",
    description="一个本地服务：IMAP 邮箱的 Web 阅读器（含会话/分类/发信）；所有 /api/* 返回 JSON。",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------- 路由装配
app.include_router(mail_router)
app.include_router(accounts_router)
app.include_router(ai_router)

# ---------------------------------------------------------------- 页面 / 静态
class VersionedStaticFiles(StaticFiles):
    """静态资源缓存策略：

    · 带 ?v=<版本号> —— 版本号取自文件 mtime，内容一变 URL 就变，
      所以可以放心 immutable 长缓存，重复访问一个字节都不用传。
    · 不带版本号 —— 退回 no-cache + ETag 协商（未变更返回 304），
      避免 Chromium 启发式缓存把旧版 app.js/index.html 多缓存几个小时不更新。
    """

    async def get_response(self, path: str, scope):
        resp = await super().get_response(path, scope)
        if scope.get("query_string"):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/static", VersionedStaticFiles(directory=str(WEB_DIR)), name="static")


def _asset_version() -> str:
    """静态资源版本号：app.js / style.css 的 mtime，改文件即自动换 URL。"""
    stamps = []
    for name in ("app.js", "style.css"):
        try:
            stamps.append(str(int((WEB_DIR / name).stat().st_mtime)))
        except OSError:
            stamps.append("0")
    return "-".join(stamps)


@app.get("/", include_in_schema=False)
def index():
    """首页：给 app.js / style.css 打上版本号，让它们能走长缓存。"""
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    v = _asset_version()
    html = html.replace("/static/style.css", f"/static/style.css?v={v}")
    html = html.replace("/static/app.js", f"/static/app.js?v={v}")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/llms.txt", include_in_schema=False)
def llms_txt():
    return PlainTextResponse(
        (WEB_DIR / "llms.txt").read_text(encoding="utf-8"),
        media_type="text/plain; charset=utf-8",
    )

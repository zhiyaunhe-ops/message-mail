"""多邮箱账号管理路由：列表 / 新增 / 修改 / 删除 / 切换 / 连通性测试。

账号配置落在 config/himalaya/config.toml（gitignore），密码落在同目录的
secret-<账号名>（同样 gitignore）。切换账号会换本地索引库并清空缓存。
"""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import settings as settings_mod
from .context import ctx
from .imap_client import IMAPClient
from .settings import ACCOUNT_NAME_RE, Account

router = APIRouter(tags=["accounts"])


class AccountIn(BaseModel):
    name: str = Field(..., description="账号标识（config.toml 里的 [accounts.<name>]）")
    email: str = ""
    display_name: str = ""
    imap_host: str = ""
    imap_port: int = 993
    imap_ssl: bool = True
    username: str = ""
    password: str = Field("", description="留空 = 不修改已有密码")
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_ssl: bool = True
    smtp_starttls: bool = False
    aliases: dict[str, str] = Field(default_factory=dict)
    default: bool = False


def _payload(a: AccountIn) -> dict:
    d = a.model_dump()
    d.pop("name", None)
    return d


@router.get("/api/accounts", summary="邮箱账号列表 + 当前选中的那个")
def list_accounts():
    try:
        items = settings_mod.list_accounts()
        active = ctx.account_name()
    except Exception as e:
        raise HTTPException(500, f"读取账号配置失败：{e}")
    return {
        "active": active,
        "count": len(items),
        "accounts": items,
        "file": str(settings_mod.CONFIG_FILE),
    }


@router.post("/api/accounts", summary="新增邮箱账号（有密码的会直接切过去）")
def create_account(a: AccountIn):
    if not ACCOUNT_NAME_RE.match((a.name or "").strip()):
        raise HTTPException(400, "账号名只能包含字母/数字/._-，长度 1-32")
    if a.name.strip() in settings_mod.accounts():
        raise HTTPException(409, f"账号 {a.name} 已存在，请换一个名字或改成编辑")
    if not a.email.strip():
        raise HTTPException(400, "邮箱地址不能为空")
    if not a.imap_host.strip():
        raise HTTPException(400, "IMAP 服务器不能为空")
    try:
        info = settings_mod.save_account(a.name.strip(), _payload(a))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"保存账号失败：{e}")

    ctx.reload_accounts()          # 配置变了，丢掉旧缓存
    # 新账号没有密码就没法连，先不切（前端会提示去补密码）
    if info.get("has_password"):
        try:
            ctx.switch_account(info["name"])
        except Exception as e:
            ctx.sync.log_add(f"新账号 {info['name']} 切换失败：{e}")
    return {"ok": True, "account": info, "active": ctx.account_name()}


@router.put("/api/accounts/{name}", summary="修改邮箱账号（password 留空则不改）")
def update_account(name: str, a: AccountIn):
    a.name = name
    if name not in settings_mod.accounts():
        raise HTTPException(404, f"账号 {name} 不存在")
    try:
        info = settings_mod.save_account(name, _payload(a))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"保存账号失败：{e}")
    ctx.reload_accounts()          # 当前账号可能被改了（服务器/密码/别名），丢掉缓存重读
    return {"ok": True, "account": info}


@router.delete("/api/accounts/{name}", summary="删除邮箱账号（purge=true 连本地索引库一起删）")
def remove_account(name: str, purge: bool = False):
    if name not in settings_mod.accounts():
        raise HTTPException(404, f"账号 {name} 不存在")
    if settings_mod.accounts().get(name, {}).get("default") or name == ctx.account_name():
        # 先切走再删，避免删掉正在用的那个
        others = [n for n in settings_mod.accounts() if n != name]
        if not others:
            raise HTTPException(400, "至少保留一个邮箱账号")
    try:
        out = settings_mod.delete_account(name, purge=purge)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"删除账号失败：{e}")
    ctx.reload_accounts()
    return {"ok": True, **out, "active": ctx.account_name()}


@router.post("/api/accounts/{name}/activate", summary="切换当前邮箱账号")
def activate_account(name: str):
    try:
        return {"ok": True, **ctx.switch_account(name)}
    except KeyError:
        raise HTTPException(404, f"账号 {name} 不存在")
    except Exception as e:
        raise HTTPException(500, f"切换失败：{e}")


@router.post("/api/accounts/{name}/test", summary="测试已保存账号的 IMAP 连通性")
def test_account(name: str):
    """用配置里的凭据真连一次 IMAP，列一遍目录，验证账号可用。"""
    try:
        acc = settings_mod.load_account(name)
    except Exception as e:
        raise HTTPException(400, f"账号不可用：{e}")
    return _probe(acc, name)


@router.post("/api/accounts/test", summary="用表单里填的凭据直连一次 IMAP（不落盘、不切账号）")
def test_credentials(a: AccountIn):
    """纯验证：不写 config.toml、不切当前账号。

    新增账号时想先试试能不能连，用这个 —— 免得「测一下」顺手把半成品账号存进去。
    密码留空则回退到该账号已保存的密码（改服务器地址时不用重新输密码）。
    """
    name = (a.name or "").strip()
    password = (a.password or "").strip()
    if not password and name in settings_mod.accounts():
        try:
            password = settings_mod.load_account(name).password
        except Exception:
            password = ""
    if not (a.imap_host or "").strip():
        raise HTTPException(400, "IMAP 服务器不能为空")
    if not password:
        raise HTTPException(400, "没有可用的密码：填一个，或先保存账号再测（保存过的密码会自动沿用）")

    acc = Account(
        name=name or "probe",
        email=(a.email or "").strip(),
        display_name=(a.display_name or "").strip(),
        imap_host=a.imap_host.strip(),
        imap_port=int(a.imap_port or 993),
        imap_ssl=bool(a.imap_ssl),
        username=(a.username or a.email or "").strip(),
        password=password,
        aliases={str(k): str(v) for k, v in (a.aliases or {}).items() if str(v or "").strip()},
    )
    return _probe(acc, name or "(未命名)")


def _probe(acc: Account, name: str) -> dict:
    """连一次 IMAP 并列出目录，返回结果或抛 HTTPException。"""
    t0 = time.time()
    try:
        client = IMAPClient(acc, timeout=20)
        client.connect()
        try:
            folders = client.list_folders()
        finally:
            client.close()
    except Exception as e:
        raise HTTPException(400, f"连接失败：{e}")
    return {
        "ok": True,
        "name": name,
        "email": acc.email,
        "elapsed": round(time.time() - t0, 2),
        "folders": len(folders),
        "samples": [f["name"] for f in folders[:8]],
    }


@router.get("/api/accounts/active", summary="当前选中的邮箱账号")
def active_account():
    acc = ctx.account()
    return {"name": acc.name, "email": acc.email, "display_name": acc.display_name}

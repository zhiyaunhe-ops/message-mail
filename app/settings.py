"""加载 himalaya 风格配置，解析出账号 / IMAP / 密码。支持多账号。

config/himalaya/config.toml 里可以写多个 ``[accounts.<名字>]``；``default = true``
的那个是默认账号，运行时用哪个由 ``app/appstate.py`` 的 ``active_account`` 决定。

密码解析优先级（按账号各取各的）：
    1. 环境变量 ``MAIL_PASSWORD_<账号名大写>``（多账号）
    2. ``imap.sasl.login.password.command`` 指向的脚本（可执行时）
    3. ``config/himalaya/secret-<账号名>``
    4. 仅默认账号：环境变量 ``MAIL_PASSWORD`` → ``config/himalaya/secret``（兼容单账号老配置）

每个账号用独立的本地库：``data/mail-<账号名>.db``。历史遗留的 ``data/mail.db``
不搬家 —— 它的归属记在 ``state.json`` 的 ``legacy_db_owner`` 里，避免 80MB 索引白重建。
"""
from __future__ import annotations

import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config" / "himalaya"
CONFIG_FILE = CONFIG_DIR / "config.toml"
SECRET_FILE = CONFIG_DIR / "secret"
DATA_DIR = ROOT / "data"
ATTACH_DIR = DATA_DIR / "attachments"
DB_FILE = DATA_DIR / "mail.db"

ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")

_config_cache: dict = {"mtime": None, "raw": {}}


# ---------------------------------------------------------------- 原始配置
def _raw_config(refresh: bool = False) -> dict:
    """解析 config.toml（按 mtime 缓存，避免每个请求都读盘 + 解析）。"""
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"未找到配置文件: {CONFIG_FILE}")
    try:
        mtime = CONFIG_FILE.stat().st_mtime
    except OSError:
        mtime = None
    if not refresh and mtime is not None and _config_cache["mtime"] == mtime and _config_cache["raw"]:
        return _config_cache["raw"]
    with CONFIG_FILE.open("rb") as fh:
        raw = tomllib.load(fh)
    _config_cache["mtime"] = mtime
    _config_cache["raw"] = raw
    return raw


def invalidate_config() -> None:
    """账号配置落盘后调用，让下一次读取重新解析。"""
    _config_cache["mtime"] = None
    _config_cache["raw"] = {}


def accounts() -> dict:
    """所有账号的原始配置：{名字: 配置 dict}。"""
    return _raw_config().get("accounts", {}) or {}


def default_account_name() -> str:
    accs = accounts()
    if not accs:
        raise ValueError("config.toml 中没有 [accounts.*] 配置")
    for k, v in accs.items():
        if isinstance(v, dict) and v.get("default"):
            return k
    return next(iter(accs))


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", (name or "").strip()) or "account"


def db_path_for(name: str) -> Path:
    """账号对应的本地索引库。老库 data/mail.db 的归属一旦确定就不再变。"""
    try:
        from . import appstate

        owner = appstate.get("legacy_db_owner")
        if not owner and DB_FILE.exists():
            owner = default_account_name()
            appstate.set("legacy_db_owner", owner)
    except Exception:
        owner = ""
    if owner and name == owner:
        return DB_FILE
    return DATA_DIR / f"mail-{_slug(name)}.db"


# ---------------------------------------------------------------- 小工具
def _dig(d: dict, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def _split_hostport(value: str) -> tuple[str, int | None]:
    host, _, port = (value or "").partition(":")
    host = host.strip()
    try:
        return host, (int(port) if port.strip() else None)
    except ValueError:
        return host, None


def _read_secret_from_command(cmd: list[str] | str) -> str | None:
    if not cmd:
        return None
    parts = cmd if isinstance(cmd, list) else [cmd]
    script = Path(parts[0])
    # 备份里是 Linux 路径（/root/.local/bin/...），Windows 上不可执行 -> 交给后面的 secret 兜底
    if not script.exists():
        return None
    try:
        if os.name == "nt" and script.suffix in {".sh", ""}:
            sh = _find_sh()
            if not sh:
                return None
            out = subprocess.run([sh, str(script)], capture_output=True, timeout=20)
        else:
            out = subprocess.run([str(script), *parts[1:]], capture_output=True, timeout=20)
        if out.returncode == 0:
            return out.stdout.decode("utf-8", "replace").strip()
    except Exception:
        return None
    return None


def _find_sh() -> str | None:
    for cand in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ):
        if Path(cand).exists():
            return cand
    return None


def _read_secret_file(name: str = "secret") -> str | None:
    for p in (CONFIG_DIR / name, CONFIG_DIR / f"{name}.txt"):
        if p.exists():
            try:
                return p.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                return None
    return None


def secret_file_for(name: str) -> Path:
    return CONFIG_DIR / f"secret-{_slug(name)}"


def _resolve_password(name: str, cfg: dict) -> str:
    env_key = "MAIL_PASSWORD_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()
    single = len(accounts()) <= 1
    is_default = bool(cfg.get("default")) or single
    candidates: list[str | None] = [os.environ.get(env_key)]
    if is_default:
        candidates.append(os.environ.get("MAIL_PASSWORD"))
    candidates.append(_read_secret_from_command(_dig(cfg, "imap", "sasl", "login", "password", "command")))
    candidates.append(_read_secret_file(f"secret-{_slug(name)}"))
    if is_default:
        candidates.append(_read_secret_file("secret"))
    for c in candidates:
        if c and str(c).strip():
            return str(c).strip()
    return ""


# ---------------------------------------------------------------- 账号
@dataclass
class Account:
    name: str
    email: str
    display_name: str
    imap_host: str
    imap_port: int
    imap_ssl: bool
    username: str
    password: str
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_ssl: bool = True
    smtp_starttls: bool = False
    aliases: dict[str, str] = field(default_factory=dict)

    @property
    def folders(self) -> list[str]:
        """需要同步的本地化文件夹列表（服务端原名）。"""
        order = ["inbox", "sent", "drafts", "archive", "junk", "trash"]
        out: list[str] = []
        for key in order:
            val = self.aliases.get(key)
            if val and val not in out:
                out.append(val)
        return out or ["INBOX"]


def load_account(name: str | None = None) -> Account:
    accs = accounts()
    if not accs:
        raise ValueError("config.toml 中没有 [accounts.*] 配置")

    if name is None or name not in accs:
        name = default_account_name()
    acc_cfg = accs[name]

    host, port_i = _split_hostport(_dig(acc_cfg, "imap", "server", default="") or "")
    port_i = port_i or 993
    ssl_on = bool(_dig(acc_cfg, "imap", "ssl", default=True))

    smtp_hostport = _dig(acc_cfg, "smtp", "server") or ""
    if smtp_hostport:
        smtp_host, sp = _split_hostport(smtp_hostport)
        smtp_port = sp or 465
    else:
        # 未显式配置 SMTP 时，用 IMAP 同主机 + 465 兜底（Coremail 即如此）
        smtp_host, smtp_port = host or None, 465

    username = _dig(acc_cfg, "imap", "sasl", "login", "username") or acc_cfg.get("email", "")
    password = _resolve_password(name, acc_cfg)
    if not password:
        raise RuntimeError(
            f"无法获取邮箱 {name} 的密码：请设置环境变量 MAIL_PASSWORD / 放 "
            f"config/himalaya/secret-{_slug(name)}，或在 WebUI「邮箱管理」里填密码"
        )

    aliases_raw = _dig(acc_cfg, "mailbox", "alias", default={}) or {}
    aliases = {str(k): str(v) for k, v in aliases_raw.items()}

    return Account(
        name=name,
        email=str(acc_cfg.get("email", "")),
        display_name=str(acc_cfg.get("display-name", "") or acc_cfg.get("email", "")),
        imap_host=host,
        imap_port=port_i,
        imap_ssl=ssl_on,
        username=str(username),
        password=password,
        smtp_host=smtp_host or None,
        smtp_port=smtp_port,
        smtp_ssl=bool(_dig(acc_cfg, "smtp", "ssl", default=True)),
        smtp_starttls=bool(_dig(acc_cfg, "smtp", "starttls", default=False)),
        aliases=aliases,
    )


def account_summary(name: str, cfg: dict | None = None) -> dict:
    """给前端看的一份账号信息（不含明文密码）。"""
    if cfg is None:
        cfg = accounts().get(name) or {}
    imap_server = _dig(cfg, "imap", "server", default="") or ""
    host, port = _split_hostport(imap_server)
    smtp_server = _dig(cfg, "smtp", "server") or ""
    shost, sport = _split_hostport(smtp_server)
    aliases = {str(k): str(v) for k, v in (_dig(cfg, "mailbox", "alias", default={}) or {}).items()}
    return {
        "name": name,
        "email": str(cfg.get("email", "")),
        "display_name": str(cfg.get("display-name", "") or cfg.get("email", "")),
        "username": str(_dig(cfg, "imap", "sasl", "login", "username") or cfg.get("email", "")),
        "imap": {"host": host, "port": port or 993, "ssl": bool(_dig(cfg, "imap", "ssl", default=True))},
        "smtp": {
            "host": shost,
            "port": sport or 465,
            "ssl": bool(_dig(cfg, "smtp", "ssl", default=True)),
            "starttls": bool(_dig(cfg, "smtp", "starttls", default=False)),
        },
        "aliases": aliases,
        "default": bool(cfg.get("default")),
        "has_password": bool(_resolve_password(name, cfg)),
        "db": str(db_path_for(name)),
    }


def list_accounts() -> list[dict]:
    return [account_summary(name, cfg) for name, cfg in accounts().items()]


# ---------------------------------------------------------------- 写
def save_account(name: str, patch: dict) -> dict:
    """新增或更新一个账号（不传的字段保持原样；password 留空 = 不修改）。"""
    name = (name or "").strip()
    if not ACCOUNT_NAME_RE.match(name):
        raise ValueError("账号名只能包含字母/数字/._-，长度 1-32")

    raw = _raw_config(refresh=True)
    accs = dict(raw.get("accounts") or {})
    acc = dict(accs.get(name) or {})
    is_new = name not in accs

    if is_new and not str(patch.get("email") or "").strip():
        raise ValueError("邮箱地址不能为空")

    if "email" in patch and str(patch["email"]).strip():
        acc["email"] = str(patch["email"]).strip()
    if "display_name" in patch:
        acc["display-name"] = str(patch["display_name"] or "").strip()
    acc.setdefault("email", "")
    acc.setdefault("display-name", acc.get("email", ""))

    # --- IMAP
    imap = dict(acc.get("imap") or {})
    cur_host, cur_port = _split_hostport(imap.get("server", "") or "")
    host = str(patch.get("imap_host") or cur_host or "").strip()
    port = int(patch.get("imap_port") or cur_port or 993)
    if host:
        imap["server"] = f"{host}:{port}"
    if "imap_ssl" in patch and patch["imap_ssl"] is not None:
        imap["ssl"] = bool(patch["imap_ssl"])
    sasl = dict(imap.get("sasl") or {})
    login = dict(sasl.get("login") or {})
    if str(patch.get("username") or "").strip():
        login["username"] = str(patch["username"]).strip()
    else:
        login.setdefault("username", acc.get("email", ""))
    sasl["login"] = login
    imap["sasl"] = sasl
    acc["imap"] = imap

    # --- SMTP
    smtp = dict(acc.get("smtp") or {})
    s_host, s_port = _split_hostport(smtp.get("server", "") or "")
    n_host = str(patch.get("smtp_host") or s_host or host or "").strip()
    n_port = int(patch.get("smtp_port") or s_port or 465)
    if n_host:
        smtp["server"] = f"{n_host}:{n_port}"
    if "smtp_ssl" in patch and patch["smtp_ssl"] is not None:
        smtp["ssl"] = bool(patch["smtp_ssl"])
    if "smtp_starttls" in patch and patch["smtp_starttls"] is not None:
        smtp["starttls"] = bool(patch["smtp_starttls"])
    acc["smtp"] = smtp

    # --- 目录别名
    if isinstance(patch.get("aliases"), dict) and patch["aliases"]:
        mailbox = dict(acc.get("mailbox") or {})
        alias = dict(mailbox.get("alias") or {})
        for k, v in patch["aliases"].items():
            if str(v or "").strip():
                alias[str(k)] = str(v).strip()
        mailbox["alias"] = alias
        acc["mailbox"] = mailbox

    # 默认账号：只有「第一个账号」或用户明确勾了「设为默认」才动 default，
    # 否则加个第二邮箱会把原来的默认账号悄悄顶掉。
    if patch.get("default") or (is_new and not accs):
        for other in accs.values():
            if isinstance(other, dict):
                other.pop("default", None)
        acc["default"] = True

    accs[name] = acc
    raw["accounts"] = accs
    text = _render(raw)
    # 落盘前自检一遍：TOML 里带点的账号名如果不加引号会被解析成嵌套表，
    # 账号会「保存成功但读不回来」。宁可在这里报错，也别静默丢账号。
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"生成的配置不是合法 TOML：{e}")
    if name not in (parsed.get("accounts") or {}):
        raise ValueError(f"账号名 {name} 写进 config.toml 后会丢失（含 TOML 特殊字符？），已中止保存")
    _write_atomic(text)

    # 密码另存到 gitignore 的独立文件；留空表示不动已有的
    pwd = str(patch.get("password") or "")
    if pwd.strip():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        secret_file_for(name).write_text(pwd.strip(), encoding="utf-8")

    invalidate_config()
    return account_summary(name)


def delete_account(name: str, purge: bool = False) -> dict:
    """删除账号配置 + 它的 secret 文件；purge=True 时连本地索引库一起删。"""
    raw = _raw_config(refresh=True)
    accs = dict(raw.get("accounts") or {})
    if name not in accs:
        raise KeyError(name)
    if len(accs) <= 1:
        raise ValueError("至少保留一个邮箱账号")

    was_default = bool((accs.get(name) or {}).get("default"))
    accs.pop(name, None)
    if was_default:
        nxt = next(iter(accs))
        accs[nxt]["default"] = True
    raw["accounts"] = accs
    _write_raw(raw)

    try:
        secret_file_for(name).unlink(missing_ok=True)
    except OSError:
        pass

    removed_db = ""
    if purge:
        p = db_path_for(name)
        if p.exists() and p != DB_FILE:
            for suffix in ("", "-wal", "-shm"):
                try:
                    Path(str(p) + suffix).unlink(missing_ok=True)
                except OSError:
                    pass
            removed_db = str(p)

    invalidate_config()
    return {"ok": True, "name": name, "removed_db": removed_db}


def set_default_account(name: str) -> None:
    raw = _raw_config(refresh=True)
    accs = dict(raw.get("accounts") or {})
    if name not in accs:
        raise KeyError(name)
    for k, v in accs.items():
        if isinstance(v, dict):
            if k == name:
                v["default"] = True
            else:
                v.pop("default", None)
    raw["accounts"] = accs
    _write_raw(raw)
    invalidate_config()


# ---------------------------------------------------------------- TOML 写
_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _fmt_key(k) -> str:
    """TOML 键：只有 A-Za-z0-9_- 能裸写，其余（如账号名 ``yonyou.com``）必须加引号。

    不加引号的 ``[accounts.yonyou.com]`` 会被解析成「accounts.yonyou 下的 com 子表」，
    第二个账号直接消失 —— 所以带点/空格的账号名一定要走这里。
    """
    s = str(k).replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "")
    return s if _BARE_KEY_RE.match(s) else f'"{s}"'


def _fmt_val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_fmt_val(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "\\n")
    return f'"{s}"'


def _dump_toml(data: dict) -> str:
    """极简 TOML 序列化：标量 / 标量数组 / 任意层子表。

    注意：会丢原文件里的注释（config.toml 已 gitignore，示例注释在 config.example.toml 里）。
    """
    lines: list[str] = []

    def walk(d: dict, prefix: str) -> None:
        for k, v in d.items():
            if isinstance(v, dict):
                continue
            lines.append(f"{_fmt_key(k)} = {_fmt_val(v)}")
        for k, v in d.items():
            if not isinstance(v, dict):
                continue
            name = f"{prefix}.{_fmt_key(k)}" if prefix else _fmt_key(k)
            lines.append("")
            lines.append(f"[{name}]")
            walk(v, name)

    walk(data, "")
    return "\n".join(lines).rstrip() + "\n"


_HEADER = """\
# 这个文件由「信箱 WebUI → 邮箱管理」读写（保存账号时会整份重写）。
# 手写注释保不住 —— 想留注释就写进同目录的 config.example.toml，或改完注释别再点保存。
# 密码不在这里：每个账号存 secret-<账号名>（默认账号也兼容 secret）。

"""


def _render(data: dict) -> str:
    """config.toml 的最终文本（头部注释 + 序列化）。"""
    return _HEADER + _dump_toml(data)


def _write_atomic(text: str) -> None:
    """写临时文件再替换，避免中途出错留下半截配置。"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(CONFIG_FILE)


def _write_raw(data: dict) -> None:
    _write_atomic(_render(data))


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ATTACH_DIR.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    for a in list_accounts():
        print(a["name"], a["email"], a["imap"], "pw=", "*" * len("x") if a["has_password"] else "-", a["db"])

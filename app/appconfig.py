"""应用级共同配置（config/app.toml）—— 与邮箱账号无关、各模块共享的设置。

和 config/himalaya/config.toml（himalaya 风格的账号配置）分开存放：
那份是「连哪个邮箱」，这份是「这个服务怎么跑」，装 AI、同步与展示设置。

读：tomllib（只读标准库） -> dict；写：一个只处理标量与子表的小序列化器，
够用且不引入 toml 依赖。带 key 的配置（api_key 等）返回时会打码。

环境变量优先级最高（CI / 临时调试用）：
    AI_BASE_URL / AI_API_KEY / AI_MODEL / AI_ENABLED / AI_TIMEOUT
    SYNC_AUTO_ENABLED / SYNC_INTERVAL_MINUTES / SYNC_AUTO_DAYS
    SYNC_STARTUP_ENABLED / SYNC_STARTUP_DAYS
    DISPLAY_UTC_OFFSET
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
APP_CONFIG_FILE = CONFIG_DIR / "app.toml"
APP_CONFIG_EXAMPLE = CONFIG_DIR / "app.example.toml"

DEFAULTS: dict = {
    "ai": {
        "enabled": True,
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o-mini",
        "temperature": 0.7,
        "timeout": 60,
        "recent_count": 10,     # 生成时参考每位收件人的最近几封邮件
        "context_chars": 1200,  # 单封邮件摘要截断长度
    },
    "sync": {
        "auto_enabled": True,        # 后台定时自动同步
        "interval_minutes": 10,      # 每隔多少分钟跑一次
        "auto_since_days": 1,        # 自动同步只回溯最近几天（新邮件够用，跑得快）
        "startup_enabled": True,     # 服务启动时先同步一次
        "startup_since_days": 7,     # 启动同步回溯最近几天（补上停机期间漏掉的）
        "with_body": True,           # 同步时连正文/附件一起抓
        "body_limit": 0,             # 单目录每轮抓正文上限，0 = 用信封上限
    },
    "display": {
        # 引用头里 Sent: 那一行按哪个时区显示。空 = 进程本地（跟以前一样）。
        # 服务跑在容器/别的时区主机上时必须显式填，否则会静默偏几个小时。
        "utc_offset": "",
    },
}

SECRET_KEYS = {"api_key", "key", "token", "secret", "password"}


# ------------------------------------------------------------------ 读
def load_raw() -> dict:
    """读配置文件，缺字段用默认值补齐；文件不存在时给一份纯默认。"""
    data: dict = {}
    if APP_CONFIG_FILE.exists():
        try:
            with APP_CONFIG_FILE.open("rb") as fh:
                data = tomllib.load(fh)
        except Exception:
            data = {}
    return _merge(DEFAULTS, data)


def _merge(base: dict, over: dict) -> dict:
    """深一层拷贝地合并 —— 必须拷贝内层 dict，否则调用方改到的是 DEFAULTS 本身。"""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for k, v in (over or {}).items():
        if isinstance(v, dict):
            out[k] = _merge(out[k], v) if isinstance(out.get(k), dict) else dict(v)
        else:
            out[k] = v
    return out


@dataclass
class AIConfig:
    enabled: bool = True
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.7
    timeout: int = 60
    recent_count: int = 10
    context_chars: int = 1200
    source: str = "default"  # default | file | env —— 诊断用

    @property
    def usable(self) -> bool:
        return bool(self.enabled and self.base_url and self.api_key and self.model)

    def public(self) -> dict:
        """给前端：不吐明文密钥，只说配没配。"""
        return {
            "enabled": self.enabled,
            "base_url": self.base_url,
            "model": self.model,
            "temperature": self.temperature,
            "timeout": self.timeout,
            "recent_count": self.recent_count,
            "context_chars": self.context_chars,
            "has_key": bool(self.api_key),
            "key_hint": _mask(self.api_key),
            "usable": self.usable,
            "source": self.source,
        }


def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return key[:2] + "*" * max(0, len(key) - 2)
    return f"{key[:4]}…{key[-4:]}"


def ai_config() -> AIConfig:
    raw = load_raw().get("ai", {})
    cfg = AIConfig(
        enabled=bool(raw.get("enabled", True)),
        base_url=str(raw.get("base_url", "") or "").strip(),
        api_key=str(raw.get("api_key", "") or "").strip(),
        model=str(raw.get("model", "") or "").strip(),
        temperature=float(raw.get("temperature", 0.7)),
        timeout=int(raw.get("timeout", 60) or 60),
        recent_count=int(raw.get("recent_count", 10) or 10),
        context_chars=int(raw.get("context_chars", 1200) or 1200),
        source="file" if APP_CONFIG_FILE.exists() else "default",
    )
    # 环境变量兜底/覆盖
    if os.environ.get("AI_BASE_URL"):
        cfg.base_url = os.environ["AI_BASE_URL"].strip()
        cfg.source = "env"
    if os.environ.get("AI_API_KEY"):
        cfg.api_key = os.environ["AI_API_KEY"].strip()
        cfg.source = "env"
    if os.environ.get("AI_MODEL"):
        cfg.model = os.environ["AI_MODEL"].strip()
        cfg.source = "env"
    if os.environ.get("AI_ENABLED"):
        cfg.enabled = os.environ["AI_ENABLED"].strip().lower() in ("1", "true", "yes", "on")
    if os.environ.get("AI_TIMEOUT"):
        try:
            cfg.timeout = int(os.environ["AI_TIMEOUT"])
        except ValueError:
            pass
    return cfg


# ------------------------------------------------------------------ 写
def save_ai(patch: dict) -> AIConfig:
    """局部更新 [ai] 段并落盘；api_key 传空串表示「不改」。"""
    data = load_raw()
    ai = dict(data.get("ai", {}))
    allowed = {
        "enabled", "base_url", "api_key", "model",
        "temperature", "timeout", "recent_count", "context_chars",
    }
    for k, v in (patch or {}).items():
        if k not in allowed or v is None:
            continue
        if k == "api_key" and isinstance(v, str) and not v.strip():
            continue  # 空串 = 保留原值（前端不回填明文密钥）
        if k in ("enabled",):
            ai[k] = bool(v)
        elif k in ("temperature",):
            ai[k] = float(v)
        elif k in ("timeout", "recent_count", "context_chars"):
            ai[k] = int(v)
        else:
            ai[k] = str(v).strip()
    data["ai"] = ai
    write_raw(data)
    return ai_config()


def write_raw(data: dict) -> None:
    """把配置写回 config/app.toml（覆盖式，但内容来自 load_raw，未登记的段会保留）。"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = APP_CONFIG_FILE.with_suffix(".toml.tmp")
    tmp.write_text(_dump_toml(data), encoding="utf-8")
    tmp.replace(APP_CONFIG_FILE)


# ------------------------------------------------------------------ 同步配置
def since_days_to_date(days: int, today: date | None = None) -> str:
    """「最近 N 天」→ 'YYYY-MM-DD'。

    用本地日期而不是 UTC：IMAP 的 SINCE 是服务器本地日期，跨时区差一天会把刚到的信漏掉，
    所以这里刻意不碰 timezone。
    """
    d = today or date.today()
    return (d - timedelta(days=max(0, int(days)))).isoformat()


def _int(v, default: int, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


@dataclass
class SyncConfig:
    auto_enabled: bool = True
    interval_minutes: int = 10
    auto_since_days: int = 1
    startup_enabled: bool = True
    startup_since_days: int = 7
    with_body: bool = True
    body_limit: int = 0
    source: str = "default"   # default | file | env —— 诊断用

    @property
    def interval_seconds(self) -> int:
        """最低 60s：定时器太密对 IMAP 服务器不友好，也没必要。"""
        return max(60, self.interval_minutes * 60)

    @property
    def auto_since(self) -> str:
        """自动同步用的 since 日期（近 N 天）。"""
        return since_days_to_date(self.auto_since_days)

    @property
    def startup_since(self) -> str:
        """启动同步用的 since 日期（近 N 天）。"""
        return since_days_to_date(self.startup_since_days)

    def public(self) -> dict:
        return {
            "auto_enabled": self.auto_enabled,
            "interval_minutes": self.interval_minutes,
            "auto_since_days": self.auto_since_days,
            "startup_enabled": self.startup_enabled,
            "startup_since_days": self.startup_since_days,
            "with_body": self.with_body,
            "body_limit": self.body_limit,
            "source": self.source,
        }


def sync_config() -> SyncConfig:
    raw = load_raw().get("sync", {})
    cfg = SyncConfig(
        auto_enabled=bool(raw.get("auto_enabled", True)),
        interval_minutes=_int(raw.get("interval_minutes"), 10, 1, 1440),
        auto_since_days=_int(raw.get("auto_since_days"), 1, 0, 3650),
        startup_enabled=bool(raw.get("startup_enabled", True)),
        startup_since_days=_int(raw.get("startup_since_days"), 7, 0, 3650),
        with_body=bool(raw.get("with_body", True)),
        body_limit=_int(raw.get("body_limit"), 0, 0, 100000),
        source="file" if APP_CONFIG_FILE.exists() else "default",
    )
    if os.environ.get("SYNC_AUTO_ENABLED") is not None:
        cfg.auto_enabled = os.environ["SYNC_AUTO_ENABLED"].strip().lower() in ("1", "true", "yes", "on")
        cfg.source = "env"
    if os.environ.get("SYNC_STARTUP_ENABLED") is not None:
        cfg.startup_enabled = os.environ["SYNC_STARTUP_ENABLED"].strip().lower() in ("1", "true", "yes", "on")
        cfg.source = "env"
    if os.environ.get("SYNC_INTERVAL_MINUTES"):
        cfg.interval_minutes = _int(os.environ["SYNC_INTERVAL_MINUTES"], cfg.interval_minutes, 1, 1440)
        cfg.source = "env"
    if os.environ.get("SYNC_AUTO_DAYS"):
        cfg.auto_since_days = _int(os.environ["SYNC_AUTO_DAYS"], cfg.auto_since_days, 0, 3650)
        cfg.source = "env"
    if os.environ.get("SYNC_STARTUP_DAYS"):
        cfg.startup_since_days = _int(os.environ["SYNC_STARTUP_DAYS"], cfg.startup_since_days, 0, 3650)
        cfg.source = "env"
    return cfg


def save_sync(patch: dict) -> SyncConfig:
    """局部更新 [sync] 段并落盘。数值越界会自动夹到合法区间（前端也做了 min/max）。"""
    data = load_raw()
    sync = dict(data.get("sync", {}))
    allowed = {
        "auto_enabled", "interval_minutes", "auto_since_days",
        "startup_enabled", "startup_since_days", "with_body", "body_limit",
    }
    for k, v in (patch or {}).items():
        if k not in allowed or v is None:
            continue
        if k in ("auto_enabled", "startup_enabled", "with_body"):
            sync[k] = bool(v)
        elif k == "interval_minutes":
            sync[k] = _int(v, 10, 1, 1440)
        elif k in ("auto_since_days", "startup_since_days"):
            sync[k] = _int(v, 1, 0, 3650)
        elif k == "body_limit":
            sync[k] = _int(v, 0, 0, 100000)
    data["sync"] = sync
    write_raw(data)
    return sync_config()


# ------------------------------------------------------------------ 展示配置
@dataclass
class DisplayConfig:
    """跟「显示成人看的时间」有关的设置。

    ``utc_offset`` 空串 = 按**进程本地时区**显示，也就是这个功能从前的行为；
    服务跑在容器或别的时区主机上时，进程本地并不等于读者本地，必须显式填。
    """

    utc_offset: str = ""       # "+08:00" / "+0800" / "+8" / "-07:00"
    source: str = "default"    # default | file | env —— 诊断用

    @property
    def tz(self):
        """解析成 tzinfo；填了但解析不出来就是 None（调用方回落进程本地）。"""
        return parse_utc_offset(self.utc_offset)

    def public(self) -> dict:
        return {
            "utc_offset": self.utc_offset,
            "effective": _offset_label(self.tz),
            "source": self.source,
        }


def parse_utc_offset(text: str):
    """``+08:00`` / ``+0800`` / ``+8`` / ``8`` -> timezone；空或非法 -> None。

    Windows 上没有系统 tz 数据库、``zoneinfo`` 连 ``Asia/Hong_Kong`` 都解析不了
    （除非额外装 tzdata），所以这里收的是**固定偏移**而不是 IANA 名字 ——
    零依赖、跨平台，对港澳/内地这种不折腾夏令时的场景也够用。
    """
    s = str(text or "").strip().replace(" ", "")
    if not s:
        return None
    m = re.fullmatch(r"([+-]?)(\d{1,2})(?::?(\d{2}))?", s)
    if not m:
        return None
    sign, hh, mm = m.group(1), int(m.group(2)), int(m.group(3) or 0)
    minutes = hh * 60 + mm
    if minutes > 14 * 60:          # UTC-12..+14，超了当填错
        return None
    if sign == "-":
        minutes = -minutes
    return timezone(timedelta(minutes=minutes))


def _offset_label(tz) -> str:
    """回显用：``+08:00`` / ``-07:00``；按进程本地时区时给空串（前端显示「服务器本地」）。"""
    if tz is None:
        return ""
    total = int((tz.utcoffset(None) or timedelta(0)).total_seconds()) // 60
    sign = "-" if total < 0 else "+"
    hh, mm = divmod(abs(total), 60)
    return f"{sign}{hh:02d}:{mm:02d}"


def display_config() -> DisplayConfig:
    raw = load_raw().get("display", {})
    cfg = DisplayConfig(
        utc_offset=str(raw.get("utc_offset") or "").strip(),
        source="file" if APP_CONFIG_FILE.exists() else "default",
    )
    env = os.environ.get("DISPLAY_UTC_OFFSET")
    if env is not None and env.strip():
        cfg.utc_offset = env.strip()
        cfg.source = "env"
    return cfg




def save_display(patch: dict) -> DisplayConfig:
    """局部更新 [display] 段并落盘。

    校验放在 API 层（填错要当场告诉用户怎么改）；这里只负责存。
    真存进来了一个解析不出来的值也不会炸：``tz`` 会是 None，也就是回落进程本地。
    """
    data = load_raw()
    disp = dict(data.get("display", {}))
    for k, v in (patch or {}).items():
        if k == "utc_offset" and v is not None:
            disp["utc_offset"] = str(v).strip()
    data["display"] = disp
    write_raw(data)
    return display_config()


def _fmt_val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_fmt_val(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\r", "").replace("\n", "\\n")
    return f'"{s}"'


def _dump_toml(data: dict) -> str:
    """极简 TOML 序列化：只支持标量 / 标量数组 / 子表，够写这份配置。"""
    lines: list[str] = []

    def walk(d: dict, prefix: str) -> None:
        for k, v in d.items():
            if isinstance(v, dict):
                continue
            lines.append(f"{k} = {_fmt_val(v)}")
        for k, v in d.items():
            if not isinstance(v, dict):
                continue
            name = f"{prefix}.{k}" if prefix else k
            lines.append("")
            lines.append(f"[{name}]")
            walk(v, name)

    walk(data, "")
    return "\n".join(lines).rstrip() + "\n"


def ensure_example() -> None:
    """首次使用生成一份示例配置（不含密钥），方便照着填。"""
    if APP_CONFIG_EXAMPLE.exists():
        return
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    example = _merge(DEFAULTS, {})
    example["ai"]["api_key"] = "sk-xxxxxx"
    APP_CONFIG_EXAMPLE.write_text(
        "# 应用级共同配置（复制为 app.toml 后生效；app.toml 已 gitignore，不会入库）\n"
        "# 也可以在 WebUI 右上角 ⚙ 设置里直接改，改完立即生效。\n"
        "#   [ai]      AI 写正文用的接口\n"
        "#   [sync]    同步策略：后台自动同步的间隔与回溯天数、启动时同步几天\n"
        "#   [display] 回复/转发引用头里 Sent: 那一行按哪个时区显示（改完下一次发信就生效，不用重启）\n"
        "#\n"
        "# 这份头和仓库里 config/app.example.toml 是两处手写副本，改一处记得改另一处。\n\n"
        + _dump_toml(example),
        encoding="utf-8",
    )

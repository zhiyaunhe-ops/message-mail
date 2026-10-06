"""极小的运行时状态文件（data/state.json）。

和 config/ 下的配置分开：
  · config/himalaya/config.toml  用户手写的账号配置
  · config/app.toml              用户手写的应用配置
  · data/state.json（本模块）     程序自己记的状态：当前选中的邮箱、老索引库归谁

data/ 已 gitignore，不会进版本库。文件很小、读写在锁里串行，出错一律退化成默认值 ——
状态丢了顶多是「回到默认账号」，不能让它把服务带崩。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "state.json"

DEFAULTS: dict = {
    "active_account": "",      # 当前选中的账号名（空 = 用 config.toml 里 default=true 的那个）
    "legacy_db_owner": "",     # data/mail.db 归哪个账号 —— 老索引库不搬家，改归属更安全
}

_lock = threading.RLock()
_cache: dict | None = None


def load() -> dict:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        data = dict(DEFAULTS)
        try:
            if STATE_FILE.exists():
                raw = json.loads(STATE_FILE.read_text(encoding="utf-8") or "{}")
                if isinstance(raw, dict):
                    data.update({k: v for k, v in raw.items() if k in DEFAULTS})
        except Exception:
            data = dict(DEFAULTS)
        _cache = data
        return data


def _flush(data: dict) -> None:
    global _cache
    _cache = data
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception:
        pass   # 状态写不进去不该让请求失败


def get(key: str, default: str = "") -> str:
    val = load().get(key)
    return val if isinstance(val, str) and val else default


def set(key: str, value: str) -> None:      # noqa: A003 - 语义清晰的短名
    with _lock:
        data = dict(load())
        data[key] = value
        _flush(data)


def reset() -> None:
    with _lock:
        _flush(dict(DEFAULTS))

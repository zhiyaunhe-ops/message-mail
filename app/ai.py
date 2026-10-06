"""AI 辅助写邮件：取上下文 -> 拼提示词 -> 调 OpenAI 兼容接口 -> 解析 3 个方案。

只用标准库（urllib），不新增依赖；任何 OpenAI 兼容端点都能用
（OpenAI / DeepSeek / 通义 / 本地 Ollama 的 /v1 等），改配置即可。

提示词是固定的（FIXED_SYSTEM / FIXED_TASK），收件人相关邮件、当前标题与正文
作为上下文拼进去；模型必须返回严格 JSON，解析失败时退回按标题分段兜底。
每个方案带一个可选的主题建议（subject），前端由用户决定要不要套用。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any

from .appconfig import AIConfig, ai_config
from .context import account, store
from .mailout import parse_addresses

MAX_TOOL_CHARS = 20000  # 上下文总长上限，防止把请求撑爆


class AIError(Exception):
    pass


FIXED_SYSTEM = (
    "你是一位资深的商务邮件写作助手，服务于一位企业信息化顾问（用友体系 ERP 项目实施）。\n"
    "你要根据「与收件人的往来邮件」和「用户正在写的草稿」，补全或改写邮件正文。\n"
    "硬性要求：\n"
    "1. 语言与参考邮件保持一致（参考邮件是中文就写中文，是英文就写英文）；称呼、落款要完整。\n"
    "2. 只输出 JSON，不要 Markdown 代码块标记，不要任何解释文字。\n"
    "3. 严禁编造事实：不确定的金额、日期、人名、系统名等一律用占位符 [请补充]。\n"
    "4. 不写空洞客套，句子要短，能落地，符合中文商务邮件习惯。\n"
    "5. 三个方案的正文内容必须明显不同（措辞/长度/态度），不要只是同义改写。\n"
    "6. 同时给每个方案配一个邮件主题，只写主题本身，不要出现「主题：」「Subject:」和前缀。"
)

FIXED_TASK = (
    "下面是与收件人相关的历史往来邮件（按时间倒序），以及用户当前写了一半的邮件。\n"
    "请据此给出 3 个不同场景下的完整正文，严格按以下 JSON 输出：\n"
    '{{"options":[{{"title":"场景名（不超过 8 个字）","subject":"主题建议（不超过 30 个字）",'
    '"body":"完整邮件正文"}},{{"title":"…","subject":"…","body":"…"}},'
    '{{"title":"…","subject":"…","body":"…"}}]}}\n'
    "三个场景默认取向（可调，但必须在 title 里点明）：\n"
    "  · 正式得体：商务正式、结构清晰，适合首次沟通或发给领导/客户高层\n"
    "  · 简洁高效：短平快、要点式，适合日常推进与内部同步\n"
    "  · 委婉推进：语气柔和、照顾对方处境，适合催办/协调/关系维护\n"
    "正文要求：含称呼与落款；段落之间用一个空行；不要带主题行；不要写 Markdown 标记。\n"
    "主题要求：一句话说清这封信要办什么事；用户已经填了主题就顺着他的意思打磨清楚，\n"
    "不要加 Re: / Fwd: / 回复 这类前缀，也不要写成「关于…的事宜」这种空话。"
)


# ------------------------------------------------------------------ 上下文
def extract_addrs(*fields: str) -> list[str]:
    """从 to/cc 等字段里抽出纯邮箱地址（小写去重）。"""
    out: list[str] = []
    for f in fields:
        for _name, addr in parse_addresses(f or ""):
            a = (addr or "").strip().lower()
            if a and a not in out:
                out.append(a)
    return out


def recent_messages(addrs: list[str], limit: int = 10) -> list[dict]:
    """与这些地址相关的最近 N 封邮件（发出或收到都算），按时间倒序。"""
    if not addrs:
        return []
    s = store()
    conds: list[str] = []
    params: list[Any] = []
    for a in addrs:
        conds.append("(m.from_addr = ? OR m.to_json LIKE ? OR m.cc_json LIKE ?)")
        params.extend([a, f"%{a}%", f"%{a}%"])
    sql = (
        "SELECT m.* FROM messages m WHERE (" + " OR ".join(conds) + ") "
        "ORDER BY m.date_ts DESC LIMIT ?"
    )
    rows = s.conn.execute(sql, [*params, int(limit)]).fetchall()
    return [type(s)._row_to_msg(r) for r in rows]


def _clean_body(text: str, limit: int) -> str:
    """去掉引用块/空行，截到 limit 字符。"""
    from .quoting import QUOTE_SEP_RE

    lines = []
    for ln in (text or "").splitlines():
        st = ln.strip()
        if st.startswith(">"):          # 引用历史（纯文本引用）
            continue
        if QUOTE_SEP_RE.match(st):      # -----Original Message----- / ----- 原始邮件 -----
            break
        lines.append(ln.rstrip())
    out = "\n".join(lines).strip()
    if len(out) > limit:
        out = out[:limit].rstrip() + "…"
    return out


def build_context(addrs: list[str], limit: int, per_chars: int) -> tuple[str, list[dict]]:
    """把历史邮件拼成给模型看的文本；返回 (文本, 明细)。"""
    msgs = recent_messages(addrs, limit)
    if not msgs:
        return "", []
    s = store()
    chunks: list[str] = []
    detail: list[dict] = []
    for i, m in enumerate(msgs, 1):
        body = _clean_body(s.get_body(m["folder"], m["uid"])["body_text"], per_chars)
        to = "、".join((t.get("email") or "") for t in (m.get("to") or [])[:4])
        chunks.append(
            f"[{i}] 时间: {m.get('date') or ''}\n"
            f"    方向: {'我发出' if (m.get('from') or {}).get('email', '').lower() == account().email.lower() else '我收到'}\n"
            f"    发件人: {(m.get('from') or {}).get('name', '')} <{(m.get('from') or {}).get('email', '')}>\n"
            f"    收件人: {to}\n"
            f"    主题: {m.get('subject') or '(无主题)'}\n"
            f"    正文: {body or '(空)'}"
        )
        detail.append({
            "folder": m["folder"], "uid": m["uid"],
            "subject": m.get("subject") or "", "date": m.get("date") or "",
            "from": (m.get("from") or {}).get("email", ""),
        })
    return "\n\n".join(chunks), detail


# ------------------------------------------------------------------ 调用
def chat_completion(
    messages: list[dict],
    cfg: AIConfig | None = None,
    temperature: float | None = None,
    timeout: int | None = None,
    max_tokens: int | None = None,
) -> str:
    """OpenAI 兼容 /chat/completions；返回 assistant 的文本。"""
    cfg = cfg or ai_config()
    if not cfg.base_url:
        raise AIError("未配置 AI 接口地址（base_url）")
    if not cfg.api_key:
        raise AIError("未配置 AI 接口密钥（api_key）")
    if not cfg.model:
        raise AIError("未配置模型名（model）")

    base = cfg.base_url.rstrip("/")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    payload: dict[str, Any] = {
        "model": cfg.model,
        "messages": messages,
        "temperature": cfg.temperature if temperature is None else temperature,
        "stream": False,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {cfg.api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout or cfg.timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise AIError(f"AI 接口返回 {e.code}：{detail or e.reason}") from e
    except urllib.error.URLError as e:
        raise AIError(f"AI 接口连接失败：{e.reason}") from e
    except Exception as e:
        raise AIError(f"AI 接口调用失败：{e}") from e

    try:
        obj = json.loads(raw)
        return ((obj.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    except Exception as e:
        raise AIError(f"AI 返回内容无法解析：{raw[:200]}") from e


def _json_loads_loose(text: str) -> Any:
    """从模型输出里抠出 JSON：优先整段，其次代码块，最后首尾花括号。"""
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s).strip()
    for cand in (s, _between(s, "{", "}")):
        if not cand:
            continue
        try:
            return json.loads(cand)
        except Exception:
            continue
    return None


def _between(s: str, open_ch: str, close_ch: str) -> str:
    i, j = s.find(open_ch), s.rfind(close_ch)
    return s[i:j + 1] if 0 <= i < j else ""


def _clean_subject(value: Any, limit: int = 60) -> str:
    """把模型给的主题收拾干净：去「主题：」前缀、去引号、压空白、限长。"""
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    s = re.sub(r"^(?:主题|标题|subject)\s*[:：]\s*", "", s, flags=re.I)
    s = s.strip().strip("\"'“”「」")
    return s[:limit].strip()


def parse_options(text: str) -> list[dict]:
    """解析成 [{title, subject, body}]；模型不听话时按「方案 N」分段兜底。

    subject 允许为空（模型没给就不给），前端只在用户勾选时才会用它填主题。
    """
    obj = _json_loads_loose(text)
    if isinstance(obj, dict):
        raw = obj.get("options") or obj.get("data") or obj.get("results")
    elif isinstance(obj, list):
        raw = obj
    else:
        raw = None

    out: list[dict] = []
    if isinstance(raw, list):
        for it in raw:
            if isinstance(it, dict):
                body = str(it.get("body") or it.get("content") or it.get("text") or "").strip()
                title = str(it.get("title") or it.get("name") or it.get("scenario") or "").strip()
                subject = _clean_subject(it.get("subject") or it.get("mail_subject") or "")
                if body:
                    out.append({
                        "title": title or f"方案 {len(out) + 1}",
                        "subject": subject,
                        "body": body,
                    })
    if out:
        return _dedup(out)[:3]

    # 兜底：按「方案 1 / 【方案一】/ Option 1」切
    parts = re.split(r"(?:^|\n)\s*(?:#+\s*)?[【\[]?(?:方案|选项|版本|Option)\s*([0-9１-９一二三]+)[\]】]?[.、:：]?\s*", text or "")
    if len(parts) >= 3:
        for i in range(1, len(parts) - 1, 2):
            body = parts[i + 1].strip()
            if body:
                out.append({"title": f"方案 {(i // 2) + 1}", "subject": "", "body": body})
    if out:
        return _dedup(out)[:3]

    body = (text or "").strip()
    return [{"title": "AI 草稿", "subject": "", "body": body}] if body else []


def _dedup(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for it in items:
        key = re.sub(r"\s+", "", it["body"])[:200]
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


# ------------------------------------------------------------------ 主流程
def draft_suggestions(
    to: str = "",
    cc: str = "",
    subject: str = "",
    body: str = "",
    cfg: AIConfig | None = None,
    recent: int | None = None,
) -> dict:
    """收件人 + 半截草稿 -> 3 个场景的正文建议。"""
    cfg = cfg or ai_config()
    if not cfg.usable:
        raise AIError("AI 未配置或未启用：请在右上角 ⚙ 设置里填写接口地址、密钥与模型")

    own = (account().email or "").strip().lower()
    addrs = [a for a in extract_addrs(to, cc) if a != own]
    limit = int(recent or cfg.recent_count or 10)
    ctx_text, detail = build_context(addrs, limit, cfg.context_chars)
    if len(ctx_text) > MAX_TOOL_CHARS:
        ctx_text = ctx_text[:MAX_TOOL_CHARS].rstrip() + "\n…（上下文已截断）"

    acc = account()
    user_parts = [
        "【我的身份】",
        f"姓名: {acc.display_name or acc.email}\n邮箱: {acc.email}",
        "",
        "【收件人】" + (to.strip() or "（未填写）"),
    ]
    if cc.strip():
        user_parts.append("【抄送】" + cc.strip())
    user_parts += [
        "",
        "【往来邮件上下文】",
        ctx_text or "（本地索引里没有找到与这些收件人的往来邮件，请按主题合理推断，不要编造细节）",
        "",
        "【我正在写的邮件】",
        f"主题: {subject.strip() or '（未填写）'}",
        "正文草稿:\n" + (body.strip() or "（尚未开始写）"),
        "",
        FIXED_TASK,
    ]

    t0 = time.time()
    text = chat_completion(
        [
            {"role": "system", "content": FIXED_SYSTEM},
            {"role": "user", "content": "\n".join(user_parts)},
        ],
        cfg=cfg,
    )
    options = parse_options(text)
    if not options:
        raise AIError("AI 没有返回可用内容，请重试或换个模型")
    return {
        "ok": True,
        "options": options,
        "model": cfg.model,
        "elapsed": round(time.time() - t0, 2),
        "context": {
            "recipients": addrs,
            "count": len(detail),
            "items": detail,
            "chars": len(ctx_text),
        },
    }


def ping(cfg: AIConfig | None = None) -> dict:
    """配置自检：发一条最小请求，确认能通。"""
    cfg = cfg or ai_config()
    if not cfg.usable:
        raise AIError("AI 未配置或未启用")
    t0 = time.time()
    text = chat_completion(
        [{"role": "user", "content": "回复两个字：可用"}],
        cfg=cfg,
        temperature=0,
        max_tokens=16,
    )
    return {"ok": True, "reply": (text or "").strip()[:80], "model": cfg.model,
            "elapsed": round(time.time() - t0, 2)}

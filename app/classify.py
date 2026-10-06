"""邮件分类：把「无聊邮件」从正常人工邮件里挑出来。

无聊邮件 = 会议/日程通知 + 系统自动生成 + 营销推广。
纯规则引擎，不联网、不调用模型，全部依据本地已有的信封 / 头部信号 / 正文片段判断。

用法：
    from app.classify import classify
    cat, boring, score, reasons = classify({
        "subject": "...", "from_addr": "noreply@x.com", "from_name": "...",
        "snippet": "...", "headers": {...}, "attachments": [...],
    })
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- 类别定义
CATEGORY_LABELS = {
    "meeting": "会议/日程",
    "automated": "系统自动",
    "promotion": "营销推广",
    "personal": "人工邮件",
}
CATEGORY_EMOJI = {
    "meeting": "📅",
    "automated": "🤖",
    "promotion": "📢",
    "personal": "✉️",
}
BORING_CATEGORIES = ("meeting", "automated", "promotion")
ALL_CATEGORIES = ("personal", "meeting", "automated", "promotion")

# 判为无聊所需的最低信号强度（低于此值一律算人工邮件，宁可放过也不错杀）
THRESHOLD_AUTO = 2.0     # 系统自动
THRESHOLD_PROMO = 2.5    # 营销推广（词最容易误伤，门槛最高）
THRESHOLD_MEETING = 2.0  # 会议（另有「强信号」判定，见 classify 末尾）


def label(category: str) -> str:
    return CATEGORY_LABELS.get(category, "人工邮件")


def is_boring_category(category: str) -> bool:
    return category in BORING_CATEGORIES


# ---------------------------------------------------------------- 信号词表
_MEETING_SUBJECT = re.compile(
    r"(会议|例会|例会邀请|会议室|日程|日程邀请|邀请函|邀请您|邀请你|参加.*会议|加入会议|会议通知|"
    r"会议邀请|会议提醒|会议变更|会议更新|会议已取消|已取消.*会议|已接受|已拒绝|已暂定|暂定|"
    r"网络研讨会|线上研讨会|培训邀请|面试邀请|预约|日程更新|日程提醒|日程变更|"
    r"invitation|invite|meeting|appointment|webinar|calendar|accepted:|declined:|tentative:|"
    r"updated invitation|canceled|cancelled|rsvp)",
    re.I,
)

_MEETING_BODY = re.compile(
    r"(加入\s*Microsoft\s*Teams\s*会议|Microsoft\s*Teams\s*meeting|Teams\s*会议|"
    r"点击此处加入会议|点击加入会议|加入会议|加入此会议|"
    r"会议\s*ID|会议号|Meeting\s*ID|会议密码|入会密码|Passcode|"
    r"腾讯会议|飞书会议|钉钉会议|Zoom\s*会议|Zoom\s*Meeting|加入\s*Zoom|"
    r"teams\.microsoft\.com/|zoom\.us/j/|meet\.google\.com/|voovmeeting|welink|"
    r"Join\s*the\s*meeting|Join\s*now|Join\s*:|会议链接|入会链接|"
    r"会议时间|会议地点|参会人|日程助理|接受邀请|拒绝邀请|Add\s*to\s*Calendar|"
    r"当地时间|Dial\s*in\s*by\s*phone|Your\s*meeting|has\s*invited\s*you|"
    r"BEGIN:VCALENDAR|已取消.*会议|会议已取消)",
    re.I,
)

_AUTO_SUBJECT = re.compile(
    r"(自动回复|自动答复|自动应答|自动通知|系统通知|系统消息|系统提醒|系统邮件|系统生成|"
    r"工单|待办提醒|待处理|审批|审批提醒|流程通知|流程提醒|会签|报账|报销提醒|"
    r"告警|报警|监控|异常提醒|健康检查|巡检|部署|构建|发布通知|流水线|流水线通知|CI\b|CD\b|"
    r"日报|周报|月报|数据报表|报表|对账单|对账|账单|发票|开票|薪资|工资条|考勤|打卡|报销单|"
    r"验证码|验证邮件|激活|注册确认|重置密码|密码修改|安全提醒|登录提醒|异地登录|"
    r"开通通知|权限|续期|到期提醒|过期提醒|域名|证书|"
    r"\bnotification\b|\bnotifications\b|\balert\b|\balarm\b|auto-?reply|out of office|do not reply|"
    r"\bticket\b|\bapproval\b|\bworkflow\b|\breminder\b|\bactivation\b|verification code|one-?time|"
    r"\binvoice\b|\breceipt\b|\bstatement\b|\breport\b|\bdigest\b|\bnewsletter\b|\bsubscription\b|"
    r"\brenewal\b|\bexpire\b|\bexpired\b|\bexpiring\b)",
    re.I,
)

_AUTO_BODY = re.compile(
    r"(本邮件由系统自动发送|系统自动发出的邮件|本邮件为系统自动|此为系统自动|由系统自动发送|"
    r"系统自动发送，请勿|请勿回复|请勿直接回复|请勿回复此邮件|不要回复|无需回复|不必回复|"
    r"此邮箱不接收回复|本邮箱不接收回复|该邮件由.*自动|邮件由.*系统自动|"
    r"Do\s*not\s*reply|do\s*not\s*respond|please\s*do\s*not\s*reply|"
    r"This\s*is\s*an\s*automated|automated\s*message|auto-?generated|system\s*generated|"
    r"unattended\s*mailbox|no\s*reply\s*necessary|这是自动发送的邮件|自动发送，无需回复)",
    re.I,
)

_AUTO_SENDER = re.compile(
    r"^(no-?reply|do-?not-?reply|donotreply|noreply|notification|notifications|notice|alert|alerts|"
    r"system|systemmail|sysmail|admin|administrator|mailer-?daemon|postmaster|mail-?daemon|bounce|bounces|"
    r"auto|automail|autosend|robot|bot|service|support|helpdesk|itsm|monitor|monitoring|devops|"
    r"ci|jenkins|gitlab|github|jira|confluence|calendar|schedule|meeting)",
    re.I,
)

_AUTO_SENDER_CONTAINS = re.compile(r"(no-?reply|donotreply|notifications?@|mailer-?daemon|auto.?reply)", re.I)

_PROMO_SUBJECT = re.compile(
    r"(优惠|促销|折扣|特价|限时|秒杀|免费领|免费领取|领取|福利|豪礼|好礼|红包|补贴|"
    r"专场|大促|年中|年末|开学季|节日|会员日|秒杀|清仓|钜惠|直降|立减| coupons?|"
    r"订阅|退订|推广|营销|广告|群发|EDM|newsletter|unsubscribe|\bsubscribe\b|"
    r"\bpromo\b|\bpromotion\b|\bdiscount\b|\bcoupon\b|\d+%\s*off|limited time|last chance|"
    r"\bsale\b|\bdeals?\b|\bspecial offer\b|\bfree\b|"
    r"网课|训练营|直播课|招生)",
    re.I,
)

# 订阅类推送的正文特征（机器账号 + 这些词 = 营销/订阅，而不是系统通知）
_FEED_RE = re.compile(
    r"(daily digest|weekly digest|\bnewsletter\b|top stories|your (daily|weekly)|"
    r"new (posts?|videos?|episodes?|articles?|items?)|unread (stories|notifications)|"
    r"推荐阅读|每日精选|本周精选|每周精选|今日推荐|热门内容|为你推荐|订阅内容|"
    r"阅读全文|点击阅读|查看原文|了解更多|立即查看)",
    re.I,
)

_PROMO_BODY = re.compile(r"(退订|取消订阅|unsubscribe|view in browser|在浏览器中查看|如需退订|邮件订阅|会员专享|立即抢购)", re.I)

_PROMO_SENDER = re.compile(r"(marketing|newsletter|promo|campaign|edm|mailer|subscri|notify-?mail|mail-?list)", re.I)

# 已知的订阅/推送服务（这类 noreply 属于推广订阅，不是系统通知）
_FEED_DOMAINS = re.compile(
    r"@(medium\.com|redditmail\.com|steampowered\.com|patreon\.com|fantia\.jp|substack\.com|"
    r"quora\.com|linkedin\.com|facebookmail\.com|twitter\.com|x\.com|youtube\.com|"
    r"netflix\.com|spotify\.com|bilibili\.com|zhihu\.com|dcard\.tw|pinterest\.com)"
)

# 明确的人工信号：一旦命中，就压低自动分类的可信度（比如领导在标题里手打了"会议纪要"）
_HUMAN_HINT = re.compile(r"(请查收|麻烦|烦请|请帮忙|请教|咨询|辛苦|谢谢|多谢|打扰|跟进|确认一下|见附件|附件请查收|FYI|fyi)", re.I)


def _norm_text(rec: dict) -> str:
    """拼一段用于关键词匹配的文本（正文不必全文，片段通常已够）。"""
    parts = [
        rec.get("subject") or "",
        rec.get("snippet") or "",
    ]
    body = rec.get("body_text") or ""
    parts.append(body[:4000])
    return "\n".join(parts)


def _signals(rec: dict) -> dict:
    headers = rec.get("headers") or {}
    sig = headers.get("signals") if isinstance(headers.get("signals"), dict) else {}
    return {
        "auto_submitted": str(sig.get("auto_submitted") or headers.get("auto_submitted") or "").strip().lower(),
        "precedence": str(sig.get("precedence") or headers.get("precedence") or "").strip().lower(),
        "list_unsubscribe": str(sig.get("list_unsubscribe") or headers.get("list_unsubscribe") or "").strip(),
        "x_autoreply": str(sig.get("x_autoreply") or headers.get("x_autoreply") or "").strip().lower(),
        "x_mailer": str(sig.get("x_mailer") or headers.get("x_mailer") or "").strip().lower(),
        "return_path": str(sig.get("return_path") or headers.get("return_path") or "").strip().lower(),
        "has_calendar": bool(sig.get("has_calendar")),
    }


def classify(rec: dict) -> tuple[str, bool, float, list[str]]:
    """返回 (category, is_boring, score, reasons)。

    rec 需要：subject / from_addr / from_name / snippet（可选 body_text）
              headers（可选，用于 Auto-Submitted 等强信号）/ attachments（可选，用于 .ics 识别）
    """
    subject = rec.get("subject") or ""
    from_addr = (rec.get("from_addr") or "").strip().lower()
    text = _norm_text(rec)
    sig = _signals(rec)
    atts = rec.get("attachments") or []

    reasons: list[str] = []
    scores = {"meeting": 0.0, "automated": 0.0, "promotion": 0.0}

    # ---- 会议 / 日程 ----
    if sig["has_calendar"] or any(
        (a.get("content_type") or "").lower().startswith("text/calendar")
        or (a.get("filename") or "").lower().endswith(".ics")
        for a in atts
    ):
        scores["meeting"] += 3.0
        reasons.append("含日历附件（.ics）")
    if _MEETING_SUBJECT.search(subject):
        scores["meeting"] += 2.0
        reasons.append("主题含会议/邀请关键词")
    if _MEETING_BODY.search(text):
        scores["meeting"] += 2.0
        reasons.append("正文含加入会议/会议号等信息")
    if re.search(r"(?i)\b(teams|zoom|webex|腾讯会议|飞书|钉钉)\b", from_addr):
        scores["meeting"] += 1.0
        reasons.append("发件人是会议系统")

    # ---- 系统自动 ----
    if sig["auto_submitted"]:
        scores["automated"] += 3.0
        reasons.append(f"Auto-Submitted: {sig['auto_submitted']}")
    if sig["x_autoreply"]:
        scores["automated"] += 3.0
        reasons.append("X-Autoreply 自动回复")
    if sig["precedence"] in ("bulk", "list", "junk", "auto_reply"):
        scores["automated"] += 1.0
        reasons.append(f"Precedence: {sig['precedence']}")
    local = from_addr.split("@")[0]
    machine = bool(local and _AUTO_SENDER.match(local)) or bool(_AUTO_SENDER_CONTAINS.search(from_addr))
    if machine:
        scores["automated"] += 2.0
        reasons.append(f"发件人是机器账号（{local}）")
    if _AUTO_SENDER_CONTAINS.search(from_addr) and not machine:
        scores["automated"] += 2.0
        reasons.append("发件地址含 noreply/notification")
    if sig["return_path"] and _AUTO_SENDER_CONTAINS.search(sig["return_path"]):
        scores["automated"] += 1.0
        reasons.append("退信地址是机器账号")
    if _AUTO_SUBJECT.search(subject):
        scores["automated"] += 1.5
        reasons.append("主题是通知/告警/报表类")
    if _AUTO_BODY.search(text):
        scores["automated"] += 2.5
        reasons.append("正文声明为系统自动发送")
    if _AUTO_BODY.search(rec.get("snippet") or ""):
        scores["automated"] += 0.5

    # ---- 营销推广 ----
    if sig["list_unsubscribe"]:
        scores["promotion"] += 1.5
        reasons.append("含 List-Unsubscribe 头")
        if sig["precedence"] in ("bulk", "list", "junk"):
            scores["promotion"] += 2.0
            reasons.append("Precedence 标记为群发")
    if _PROMO_SUBJECT.search(subject):
        scores["promotion"] += 2.0
        reasons.append("主题含促销/订阅关键词")
    if _PROMO_BODY.search(text):
        scores["promotion"] += 1.5
        reasons.append("正文含退订/订阅链接")
    if _PROMO_SENDER.search(from_addr):
        scores["promotion"] += 2.0
        reasons.append("发件地址是营销通道")

    # 机器账号 + 订阅/营销内容：这类是「推送/营销」而不是「系统通知」
    if machine:
        if _PROMO_BODY.search(text):
            scores["promotion"] += 2.5
            reasons.append("机器账号 + 退订/订阅链接")
        if _FEED_RE.search(text):
            scores["promotion"] += 2.0
            reasons.append("订阅类推送（digest/精选/推荐）")
        if _PROMO_SUBJECT.search(subject):
            scores["promotion"] += 1.5
            reasons.append("机器账号 + 促销/订阅主题")
        if _FEED_DOMAINS.search(from_addr):
            scores["promotion"] += 2.0
            reasons.append("发件方是订阅/推送服务")

    # ---- 人工信号：抵消一部分自动判定（避免把"会议纪要请查收"判成会议通知）----
    human = bool(_HUMAN_HINT.search(subject)) or bool(_HUMAN_HINT.search((rec.get("snippet") or "")[:200]))
    if human:
        scores["automated"] -= 1.0
        scores["promotion"] -= 1.0
        scores["meeting"] -= 1.0
        reasons.append("含人工语气（请查收/麻烦等）")

    # ---- 裁决：会议 > 系统自动 > 营销推广 ----
    best = max(("meeting", "automated", "promotion"), key=lambda k: scores[k])
    best_score = scores[best]

    # 会议类要求强信号：光是正文里带了个 Teams 链接不足以把人工邮件判成会议通知
    if best == "meeting":
        strong = (
            sig["has_calendar"]
            or scores["meeting"] >= 4.0
            or (bool(_MEETING_SUBJECT.search(subject)) and scores["meeting"] >= 3.0)
        )
        if not strong:
            best = max(("automated", "promotion"), key=lambda k: scores[k])
            best_score = scores[best]

    need = {"meeting": THRESHOLD_MEETING, "automated": THRESHOLD_AUTO, "promotion": THRESHOLD_PROMO}[best]
    if best_score < need:
        return "personal", False, round(best_score, 2), reasons
    return best, True, round(best_score, 2), reasons

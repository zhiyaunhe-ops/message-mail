"""引用头时区（config/app.toml 的 [display] utc_offset）的回归测试：
python scripts/test_display_tz.py

防的是这类事故：服务跑在 UTC 容器 / 别的时区主机上，回复里那行
「Sent: Monday, 15 September 2026 09:02」被印成 01:02 —— 静默错 8 小时，
而且错在发出去给对方看的信里，本机跑不出来、也看不出来。

覆盖：
- 解析     ：+08:00 / +0800 / +8 / 8 / -07:00 / 空 / IANA 名 / 越界 / 垃圾串
- 配置往返 ：写进 [display] utc_offset 能读回来，读不到时不炸（回落进程本地）
- 端点     ：PUT /api/config/display 填错要 400，且原话说明该怎么改
- 端到端   ：配置生效后 build_quote 出来的引用头（纯文本 + HTML）就是那个时区

**不会碰真实的 config/app.toml** —— 整个过程把配置路径指到临时目录，
真配置里有 API Key，测试不该去写它。
"""
from __future__ import annotations

import sys
import tempfile
from datetime import timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import appconfig, mailparse, quoting  # noqa: E402
from app.api_ai import DisplayConfigPatch, get_display_config, put_display_config  # noqa: E402

PASS = FAIL = 0


def ok(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}")


def eq(got, want, label):
    ok(got == want, f"{label}" + ("" if got == want else f"   期望 {want!r}，实得 {got!r}"))


REAL_CONFIG = appconfig.APP_CONFIG_FILE
REAL_DIR = appconfig.CONFIG_DIR
REAL_EXAMPLE = appconfig.APP_CONFIG_EXAMPLE


def use_temp_config():
    """把配置读写整体指到临时目录，绝不写用户真实的 app.toml。"""
    tmp = Path(tempfile.mkdtemp(prefix="msgcli-tz-"))
    appconfig.CONFIG_DIR = tmp
    appconfig.APP_CONFIG_FILE = tmp / "app.toml"
    appconfig.APP_CONFIG_EXAMPLE = tmp / "app.example.toml"
    return tmp


def restore_config():
    appconfig.CONFIG_DIR = REAL_DIR
    appconfig.APP_CONFIG_FILE = REAL_CONFIG
    appconfig.APP_CONFIG_EXAMPLE = REAL_EXAMPLE


HKT = timezone(timedelta(hours=8))
UTC = timezone(timedelta(0))
PDT = timezone(timedelta(hours=-7))

try:
    # ------------------------------------------------------------ 解析
    print("\n[解析：收固定偏移，不收 IANA 名]")
    eq(appconfig.parse_utc_offset("+08:00"), HKT, "+08:00")
    eq(appconfig.parse_utc_offset("+0800"), HKT, "+0800")
    eq(appconfig.parse_utc_offset("+8"), HKT, "+8")
    eq(appconfig.parse_utc_offset("8"), HKT, "裸数字 8 当 +08:00")
    eq(appconfig.parse_utc_offset("+08:30"), timezone(timedelta(hours=8, minutes=30)), "半小时偏移也认")
    eq(appconfig.parse_utc_offset("-07:00"), PDT, "-07:00")
    eq(appconfig.parse_utc_offset(" -07:00 "), PDT, "前后空格无所谓")
    eq(appconfig.parse_utc_offset(""), None, "空 = 不指定")
    eq(appconfig.parse_utc_offset(None), None, "None 也不炸")
    eq(appconfig.parse_utc_offset("Asia/Hong_Kong"), None, "IANA 名不收（Windows 上解析不了）")
    eq(appconfig.parse_utc_offset("+20:00"), None, "超过 +14:00 当填错")
    eq(appconfig.parse_utc_offset("+14:00"), timezone(timedelta(hours=14)), "+14:00 是上界，合法")
    eq(appconfig.parse_utc_offset("garbage"), None, "垃圾串当没填")

    # ------------------------------------------------------------ 没配 = 老行为
    print("\n[没配 utc_offset：行为跟以前完全一样]")
    use_temp_config()
    eq(appconfig.display_config().utc_offset, "", "没有配置文件时 utc_offset 是空")
    eq(appconfig.display_config().tz, None, "tz 是 None -> 调用方回落进程本地")
    eq(appconfig.display_config().source, "default", "来源标 default")
    eq(appconfig.display_config().public()["effective"], "", "回显为空（前端显示「服务器本地时区」）")

    # ------------------------------------------------------------ 配置往返
    print("\n[写进 [display] 再读回来]")
    cfg = appconfig.save_display({"utc_offset": "+08:00"})
    eq(cfg.utc_offset, "+08:00", "返回值就是刚写的")
    eq(cfg.tz, HKT, "解析成 tzinfo")
    eq(cfg.public()["effective"], "+08:00", "回显成 +08:00")
    eq(appconfig.display_config().utc_offset, "+08:00", "重新读一遍还在（真落盘了）")
    eq(appconfig.display_config().source, "file", "有配置文件时来源标 file")

    # 别把 [ai] / [sync] 段写丢了
    appconfig.save_display({"utc_offset": "+08:00"})
    raw = appconfig.load_raw()
    ok("ai" in raw and "sync" in raw, "写 [display] 不会把别的段写丢")

    appconfig.save_display({"utc_offset": ""})
    eq(appconfig.display_config().tz, None, "清空后回到不指定")
    appconfig.save_display({"utc_offset": "-07:00"})
    eq(appconfig.display_config().tz, PDT, "改成 -07:00 也生效")

    # ------------------------------------------------------------ 端点
    print("\n[端点：填错要当场说清楚]")
    r = put_display_config(DisplayConfigPatch(utc_offset="+09:00"))
    eq(r["ok"], True, "合法偏移存得下去")
    eq(r["config"]["effective"], "+09:00", "响应里带生效值")
    eq(get_display_config()["config"]["effective"], "+09:00", "GET 读得到同一个值")

    for bad in ("Asia/Hong_Kong", "+20:00", "八百", "08:00x"):
        try:
            put_display_config(DisplayConfigPatch(utc_offset=bad))
            ok(False, f"{bad!r} 应该被拒")
        except Exception as e:
            detail = getattr(e, "detail", str(e))
            status = getattr(e, "status_code", None)
            ok(status == 400 and "+08:00" in detail,
               f"{bad!r} -> 400 且说明写法（{detail[:38]}…）")

    put_display_config(DisplayConfigPatch(utc_offset=""))
    eq(get_display_config()["config"]["effective"], "", "留空 = 恢复按服务器本地时区")
    eq(get_display_config()["file"], "config/app.toml", "响应指出配置文件在哪")

    # ------------------------------------------------------------ 端到端
    print("\n[端到端：配置 -> 引用头的 Sent:]")
    orig = {
        "subject": "BIP / OA integration requirement study",
        "from": {"name": "Anthony Sample (UNIT-FIN)", "email": "sender.sample@example.com"},
        "to": [{"name": "Sender Sample", "email": "me@example.com"}],
        "date": "2026-09-15T01:00:28+00:00",     # 对方在 UTC 发的
    }
    body = {"body_text": "BIP suggest having a meeting today 10:30 am", "body_html": ""}

    put_display_config(DisplayConfigPatch(utc_offset="+08:00"))
    tz = appconfig.display_config().tz
    eq(quoting.fmt_quote_time(orig["date"], tz), "Tuesday, 15 September 2026 09:00",
       "按 +08:00 印成 09:00")
    text, html_body, _ = quoting.build_quote("收到，我确认一下", orig, body, [], tz=tz)
    ok("Sent: Tuesday, 15 September 2026 09:00" in text, "纯文本引用头 09:00")
    ok("09:00" in html_body, "HTML 引用头 09:00")
    ok("01:00" not in text, "不会残留服务器算出来的 01:00")

    # 换成 UTC：同一封信必须换一个数，证明配置真的在起作用
    put_display_config(DisplayConfigPatch(utc_offset="+00:00"))
    tz2 = appconfig.display_config().tz
    t2, _, _ = quoting.build_quote("ok", orig, body, [], tz=tz2)
    ok("Sent: Tuesday, 15 September 2026 01:00" in t2, "改成 +00:00 后引用头变成 01:00")

    # 留空：回落进程本地 —— 老行为，本机 +08:00 时也印 09:00
    put_display_config(DisplayConfigPatch(utc_offset=""))
    local = appconfig.display_config().tz
    eq(local, None, "留空时 tz 是 None（回落进程本地）")
    t3, _, _ = quoting.build_quote("ok", orig, body, [], tz=local)
    ok("Sent: Tuesday, 15 September 2026" in t3, "回落路径照样出引用头")

    # ------------------------------------------------------------ naive 值
    print("\n[naive 值（没有偏移）：不猜时区]")
    # 这条路径是真实存在的：sync.py 在邮件没有 Date: 头时退回 IMAP INTERNALDATE，
    # 而 mailparse._INTERNALDATE_FMT 里有一个不带 %z 的写法 -> date_iso 会没有偏移。
    naive_iso, _ts = mailparse.parse_internaldate("15-Sep-2026 09:00:00")
    eq(naive_iso, "2026-09-15T09:00:00", "INTERNALDATE 兜底确实会产出 naive 的 date_iso")
    from datetime import datetime as _dt

    ok(_dt.fromisoformat(naive_iso).tzinfo is None, "确认它没有 tzinfo")

    # 时区未知就不动它。按进程本地再折一次 = 凭空发明一个偏移，正是这次要修的毛病。
    for tzname, z in (("+08:00", HKT), ("+00:00", UTC), ("-07:00", PDT), ("不传", None)):
        eq(quoting.fmt_quote_time(naive_iso, z), "Tuesday, 15 September 2026 09:00",
           f"naive 值在 tz={tzname} 下原样 09:00（不偏移）")

finally:
    restore_config()

print(f"\n通过 {PASS} / 失败 {FAIL}")
sys.exit(1 if FAIL else 0)

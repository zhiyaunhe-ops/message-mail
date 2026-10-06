#!/usr/bin/env python
"""Tailnet 多应用曝光助手 —— 一条命令把本机 127.0.0.1:PORT 挂上 Tailscale。

背后就一条命令：

    tailscale serve --bg --https=<tailnet端口> http://127.0.0.1:<本地端口>

好处：Tailscale 自动签证书 + 自动续期，只在 tailnet 内可见，**不用改应用代码**，
也不用加 Windows 防火墙规则（Tailscale 自己那条 Tailscale-In 规则已经放行到
100.x.y.z 的任意端口）。每个应用占一个 tailnet 端口，配一份登记表，
重建/换机时 `sync` 一把恢复。

    python scripts/tailnet.py add mail 8791 --note "邮件 WebUI"
    python scripts/tailnet.py add git 3000              # tailnet 端口自动分配
    python scripts/tailnet.py list
    python scripts/tailnet.py url mail
    python scripts/tailnet.py remove git
    python scripts/tailnet.py sync                      # 按登记表全量重下发，修漂移

登记表落在 config/tailnet_apps.json（只有名字和端口，不含任何密钥，可以入库）。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import socket
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REG = ROOT / "config" / "tailnet_apps.json"

# HTTPS 端口自动分配顺序：先抢 443，再 8443 / 9443 / 10443 …
PORT_CANDIDATES = [443] + [8443 + i * 1000 for i in range(20)]

_TS = None


def ts(*args: str) -> subprocess.CompletedProcess:
    """调用 tailscale CLI。"""
    global _TS
    if _TS is None:
        _TS = _find_ts()
    return subprocess.run(
        [_TS, *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


def _find_ts() -> str:
    cands = [
        shutil.which("tailscale"),
        r"C:\Program Files\Tailscale\tailscale.exe",
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
        shutil.which("tailscale.exe"),
    ]
    for c in cands:
        if not c:
            continue
        try:
            if subprocess.run([c, "version"], capture_output=True).returncode == 0:
                return c
        except OSError:
            continue
    raise SystemExit("找不到 tailscale 命令，请确认已安装并在 PATH 里")


def load() -> dict:
    if not REG.exists():
        return {"apps": {}}
    return json.loads(REG.read_text(encoding="utf-8"))


def reserved_ports(cfg: dict) -> dict[int, str]:
    """登记表里声明为「外部占用」的 tailnet 端口 -> 说明。

    这些端口由别的工具管理（如 TailShare 的 tailscale serve），
    add 不得选用，sync 不得下发、也不得当垃圾清掉。
    """
    out: dict[int, str] = {}
    for port, meta in (cfg.get("external_serves") or {}).items():
        try:
            p = int(port)
        except (TypeError, ValueError):
            continue
        note = meta.get("note", "") if isinstance(meta, dict) else str(meta)
        out[p] = note
    return out


def save(cfg: dict) -> None:
    REG.parent.mkdir(parents=True, exist_ok=True)
    REG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def node_name() -> str:
    p = ts("status", "--json")
    if p.returncode != 0:
        raise SystemExit(f"读取 tailscale 状态失败：{(p.stderr or p.stdout).strip()}")
    return (json.loads(p.stdout)["Self"]["DNSName"] or "").rstrip(".")


def actual() -> dict[int, str]:
    """从 tailscale serve status --json 抽出 {tailnet端口: 上游proxy}。"""
    p = ts("serve", "status", "--json")
    if p.returncode != 0 or not (p.stdout or "").strip():
        return {}
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        return {}
    out: dict[int, str] = {}
    for hostport, web in (data.get("Web") or {}).items():
        handler = (web.get("Handlers") or {}).get("/") or {}
        proxy = handler.get("Proxy")
        if not proxy:
            continue
        try:
            out[int(hostport.rsplit(":", 1)[-1])] = proxy
        except ValueError:
            continue
    return out


def listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def url_of(node: str, port: int) -> str:
    return f"https://{node}/" if port == 443 else f"https://{node}:{port}/"


def pick_port(cfg: dict, local_port: int) -> int:
    apps = cfg.get("apps", {})
    act = actual()
    if not apps:
        cur = act.get(443)
        if cur is None or cur.endswith(f":{local_port}"):
            return 443
    taken = {a["tailnet_port"] for a in apps.values()} | set(act) | set(reserved_ports(cfg))
    for p in PORT_CANDIDATES[1:]:
        if p not in taken:
            return p
    raise SystemExit("端口候选耗尽，去脚本里扩 PORT_CANDIDATES")


def warn_public_profile() -> None:
    """Tailscale-In 防火墙规则只覆盖「域/专用」配置文件，网络被判成「公用」时会挡掉入站。"""
    try:
        p = subprocess.run(
            ["netsh", "advfirewall", "show", "currentprofile"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        text = p.stdout or ""
        if "公用" in text[:200] or "Public profile" in text[:200]:
            print("  注意：当前网络被 Windows 归为「公用」，Tailscale 的入站放行规则只在「域/专用」生效，"
                  "外部设备可能连不上。把该网络改成「专用」。")
    except OSError:
        pass


def cmd_add(args) -> None:
    name = args.name.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}", name):
        raise SystemExit("应用名只能用小写字母 / 数字 / 短横线，且以字母或数字开头")

    cfg = load()
    apps = cfg.setdefault("apps", {})
    if name in apps and not args.force:
        old = apps[name]
        raise SystemExit(f"'{name}' 已登记（本地 :{old['local_port']}）。要改端口就加 --force")

    local = args.local_port
    if not listening(local):
        print(f"  警告：127.0.0.1:{local} 上没有监听 —— 先把应用起起来，否则挂上去是 502")

    port = args.port or pick_port(cfg, local)
    if any(a["tailnet_port"] == port for k, a in apps.items() if k != name):
        raise SystemExit(f"tailnet 端口 {port} 已被别的应用占用")
    if port in reserved_ports(cfg):
        raise SystemExit(f"tailnet 端口 {port} 登记为外部占用（{reserved_ports(cfg)[port]}），换个端口")

    p = ts("serve", "--bg", f"--https={port}", f"http://127.0.0.1:{local}")
    if p.returncode != 0:
        raise SystemExit(f"tailscale serve 失败：{(p.stderr or p.stdout).strip()}")

    apps[name] = {
        "local_port": local,
        "tailnet_port": port,
        "note": args.note or "",
    }
    save(cfg)
    print(f"  已挂载 {name}：{url_of(node_name(), port)}")
    print(f"    127.0.0.1:{local}  ->  tailnet :{port}")
    warn_public_profile()


def cmd_remove(args) -> None:
    cfg = load()
    apps = cfg.get("apps", {})
    if args.name not in apps:
        raise SystemExit(f"登记表里没有 '{args.name}'，先跑 list 看看")
    app = apps.pop(args.name)
    ts("serve", f"--https={app['tailnet_port']}", "off")
    save(cfg)
    print(f"  已摘除 {args.name}（tailnet :{app['tailnet_port']} 已关闭）")


def cmd_list(args) -> None:
    cfg = load()
    apps = cfg.get("apps", {})
    reserved = reserved_ports(cfg)
    node = node_name()
    act = actual()

    if not apps:
        print("还没有登记任何应用。加一个试试：")
        print("  python scripts/tailnet.py add mail 8791 --note \"邮件 WebUI\"")
    else:
        width = max(len(k) for k in apps)
        for name, a in sorted(apps.items(), key=lambda kv: kv[1]["tailnet_port"]):
            cur = act.get(a["tailnet_port"])
            if cur is None:
                state = "未下发"
            elif cur.endswith(f":{a['local_port']}"):
                state = "在线" if listening(a["local_port"]) else "已配置但本地没监听"
            else:
                state = f"漂移（实际指向 {cur}）"
            note = f"  {a['note']}" if a.get("note") else ""
            print(f"  {name:<{width}}  本地 :{a['local_port']:<6} {url_of(node, a['tailnet_port']):<42} {state}{note}")

    known = {a["tailnet_port"] for a in apps.values()}
    if reserved:
        print("\n外部保留（别的工具管理，sync 不下发也不清理）：")
        for port, note in sorted(reserved.items()):
            cur = act.get(port)
            state = "在线" if cur else "未下发"
            print(f"  :{port:<6} {state}  {note}")
    extra = {p: v for p, v in act.items() if p not in known and p not in reserved}
    if extra:
        print("\n未登记的 serve 配置（手工加的，用 tailscale serve --https=PORT off 清理）：")
        for port, proxy in sorted(extra.items()):
            print(f"  :{port}  ->  {proxy}")


def cmd_url(args) -> None:
    apps = load().get("apps", {})
    if args.name not in apps:
        raise SystemExit(f"没有登记 '{args.name}'")
    print(url_of(node_name(), apps[args.name]["tailnet_port"]))


def cmd_sync(args) -> None:
    """按登记表全量重下发：补缺、修漂移、清掉没登记的（外部保留端口除外）。"""
    cfg = load()
    apps = cfg.get("apps", {})
    reserved = reserved_ports(cfg)
    act = actual()
    want = {a["tailnet_port"]: a["local_port"] for a in apps.values()}
    changed = 0

    for port, local in sorted(want.items()):
        if port in reserved:
            print(f"  跳过 :{port} —— 登记为外部占用（{reserved[port]}），先改登记表再下发")
            continue
        if act.get(port) != f"http://127.0.0.1:{local}":
            p = ts("serve", "--bg", f"--https={port}", f"http://127.0.0.1:{local}")
            if p.returncode != 0:
                print(f"  失败 :{port} -> 127.0.0.1:{local}：{(p.stderr or p.stdout).strip()}")
                continue
            print(f"  下发 :{port} -> 127.0.0.1:{local}")
            changed += 1

    for port in sorted(act):
        if port not in want and port not in reserved:
            ts("serve", f"--https={port}", "off")
            print(f"  清理未登记的 :{port}")
            changed += 1

    print("  已是最新，没有改动" if not changed else f"  完成，共改动 {changed} 项")


def main() -> None:
    p = argparse.ArgumentParser(
        description="把本机 127.0.0.1:PORT 的应用挂到 Tailscale（HTTPS + 自动证书 + 仅 tailnet 可见）"
    )
    sub = p.add_subparsers(dest="cmd")

    a = sub.add_parser("add", help="登记并挂载一个应用")
    a.add_argument("name", help="应用名，如 mail / git / next")
    a.add_argument("local_port", type=int, help="应用在本机监听的端口")
    a.add_argument("--port", type=int, default=None, help="指定 tailnet 端口（默认自动分配）")
    a.add_argument("--note", default="", help="备注")
    a.add_argument("--force", action="store_true", help="覆盖已登记的同名应用")
    a.set_defaults(func=cmd_add)

    r = sub.add_parser("remove", help="摘除一个应用")
    r.add_argument("name")
    r.set_defaults(func=cmd_remove)

    l = sub.add_parser("list", help="列出登记的应用与在线状态")
    l.set_defaults(func=cmd_list)

    u = sub.add_parser("url", help="打印某个应用的访问地址")
    u.add_argument("name")
    u.set_defaults(func=cmd_url)

    s = sub.add_parser("sync", help="按登记表全量重下发（补缺 / 修漂移 / 清未登记）")
    s.set_defaults(func=cmd_sync)

    args = p.parse_args()
    if not getattr(args, "func", None):
        args.func, args.name = cmd_list, None
    args.func(args)


if __name__ == "__main__":
    main()

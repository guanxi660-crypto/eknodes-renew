#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EkNodes 免费服务器自动续期
================================
原理：
  EkNodes 面板后端使用 Supabase。点击面板里的 "Renovar"(续期) 按钮最终只做两件事：
    1. 通过 Cloudflare Turnstile 人机验证
    2. 把该用户在 supabase 表 servers.expires_at 更新为 续期周期之后的时间

  免费计划（Plan Free / renewal_period=weekly）续期 = expires_at = now + 7 天。
  因此本脚本直接调用 Supabase 的 GoTrue 密码登录 + PostgREST 更新到期时间，
  达到与点击 "Renovar" 完全相同的结果，且不需要处理 Turnstile。

  仅自动续期免费计划（price_monthly == 0），付费计划跳过（避免误扣 coins）。

可选功能：
  设置 PROXY_URL（如 socks5://127.0.0.1:10808）后，所有请求会走 SOCKS5 代理出口，
  通常用于 GitHub Actions 里配合 VLESS_NODE 绕开机房 IP 限制（需要 pip install pysocks）。

  Supabase 的地址与访问凭据无需填写：留空时会自动从面板前端页面探测获取。
  若面板对当前出口 IP 做了拦截导致探测失败，可先用上面的代理，
  或用 EK_SUPABASE_URL / EK_SUPABASE_ANON 手动指定（需成对填写）。
"""

import os
import re
import sys
import json
import base64
import socket
import argparse
import datetime
import urllib.request
import urllib.parse
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# 配置
# ============================================================

# Supabase 地址与访问凭据：设置了环境变量就用环境变量，留空则运行时从面板前端自动探测
PANEL_ORIGIN = os.environ.get("EK_PANEL_URL", "https://dash.eknodes.es").rstrip("/")
SUPABASE_URL = ""
ANON_KEY = ""

EMAIL = os.environ.get("EK_EMAIL", "").strip()
PASSWORD = os.environ.get("EK_PASSWORD", "").strip()

# 距到期不足该小时数时才触发续期（默认 48 小时）
_threshold_raw = os.environ.get("EK_RENEW_THRESHOLD_HOURS", "").strip()
THRESHOLD_HOURS = float(_threshold_raw) if _threshold_raw else 48.0
# 可选 Telegram 通知：TG_BOT=chat_id,bot_token
_tg = os.environ.get("TG_BOT", "").split(",")
TG_CHAT_ID = _tg[0].strip() if len(_tg) > 0 else ""
TG_TOKEN = _tg[1].strip() if len(_tg) > 1 else ""
# 可选出口代理，如 socks5://127.0.0.1:10808
PROXY_URL = os.environ.get("PROXY_URL", "").strip()

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def fmt_dt(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")


def parse_expiry(s: str) -> datetime.datetime:
    """解析 expires_at。样例: 2026-09-10T06:16:16.771+00:00"""
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        # 兜底：去掉微秒
        base = s.split(".")[0]
        if base.endswith("Z"):
            base = base[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(base)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


# ============================================================
# SOCKS5 出口代理（可选）
# ============================================================

def enable_proxy(proxy_url: str) -> bool:
    """把进程内所有 TCP 连接切到 SOCKS5 代理（需安装 PySocks）。"""
    if not proxy_url:
        return False
    try:
        import socks  # PySocks
    except ImportError:
        print("❌ 已设置 PROXY_URL，但缺少 pysocks，请先安装：pip install pysocks")
        sys.exit(1)
    p = urllib.parse.urlsplit(proxy_url if "://" in proxy_url else "socks5://" + proxy_url)
    host = p.hostname or "127.0.0.1"
    port = p.port or 1080
    socks.set_default_proxy(socks.SOCKS5, host, port, rdns=True)
    socket.socket = socks.socksocket
    print(f"🔌 已启用 SOCKS5 出口代理: {host}:{port}")
    return True


# ============================================================
# Supabase 信息自动探测（面板页面 / 前端 JS）
# ============================================================

_MAX_SCRIPTS = 40          # 最多探测的前端脚本数量
_SCRIPT_BYTES = 2_000_000  # 单个脚本最多读取的字节数

_SUPA_RE = re.compile(r"https://([a-z0-9]{15,30})\.supabase\.co", re.I)
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}")


def _b64url_json(seg: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)).decode("utf-8", "ignore"))


def _anon_ref(token: str) -> str:
    """校验 JWT 是否为 supabase 的 anon 凭据，是则返回其项目 ref，否则返回空串"""
    try:
        payload = _b64url_json(token.split(".")[1])
    except Exception:
        return ""
    if payload.get("role") != "anon":
        return ""
    return str(payload.get("ref") or "")


def _extract(text: str):
    """从文本中提取 (supabase_url, anon_key)，提取不到则返回 (None, None)"""
    refs = _SUPA_RE.findall(text)
    anons = [(t, _anon_ref(t)) for t in _JWT_RE.findall(text)]
    anons = [(t, r) for t, r in anons if r]
    if not refs or not anons:
        return None, None
    for ref in refs:                       # 优先取地址与 ref 一致的组合
        for token, tref in anons:
            if tref == ref:
                return f"https://{ref}.supabase.co", token
    return "https://%s.supabase.co" % anons[0][1], anons[0][0]


def _fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"user-agent": UA, "accept": "*/*"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read(_SCRIPT_BYTES).decode("utf-8", "ignore")


def _script_urls(html: str):
    """从 HTML 中提取 <script src=...>，去重，并按「越像业务代码越靠前」排序"""
    urls, seen = [], set()
    for m in re.finditer(r"<script[^>]+src=[\"']([^\"']+)[\"']", html, re.I):
        u = urllib.parse.urljoin(PANEL_ORIGIN + "/", m.group(1))
        if u not in seen:
            seen.add(u)
            urls.append(u)

    def score(u: str) -> int:
        name = u.lower()
        if "/chunks/app/" in name or "page-" in name:
            return 0
        return 1 if "/chunks/" in name else 2

    urls.sort(key=score)
    return urls[:_MAX_SCRIPTS]


def probe_supabase():
    """从面板前端页面与其 JS 中探测 supabase 地址与 anon 凭据"""
    print(f"🔎 正在从面板探测所需信息: {PANEL_ORIGIN}")
    try:
        html = _fetch_text(PANEL_ORIGIN + "/servers")
    except Exception as e:
        print(f"⚠️ 面板页面获取失败: {e}")
        html = ""

    url, key = _extract(html)
    if not (url and key):
        scripts = _script_urls(html)
        if not scripts:
            print("⚠️ 未能从页面解析出前端脚本")
        else:
            print(f"   检查 {len(scripts)} 个前端脚本 ...")
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = {pool.submit(_fetch_text, s): s for s in scripts}
                for fut in as_completed(futures):
                    try:
                        text = fut.result()
                    except Exception:
                        continue
                    u, k = _extract(text)
                    if u and k:
                        url, key = u, k
                        for f in futures:
                            f.cancel()
                        break
    if url and key:
        print(f"✅ 探测成功: {url}")
        return url, key
    print("❌ 探测失败")
    return None


def resolve_credentials():
    """确定 SUPABASE_URL / ANON_KEY：环境变量优先，留空则自动探测（须在代理生效后调用）"""
    global SUPABASE_URL, ANON_KEY
    url = os.environ.get("EK_SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("EK_SUPABASE_ANON", "").strip()
    if url and key:
        SUPABASE_URL, ANON_KEY = url, key
        return
    if url or key:
        print("⚠️ EK_SUPABASE_URL / EK_SUPABASE_ANON 需成对填写，已忽略并改用自动探测")
    found = probe_supabase()
    if not found:
        sys.exit(
            "❌ 无法自动探测到所需信息。请设置 VLESS_NODE 换个出口网络后重试，"
            "或手动设置 EK_SUPABASE_URL 与 EK_SUPABASE_ANON 两个环境变量。"
        )
    SUPABASE_URL, ANON_KEY = found


# ============================================================
# HTTP 工具（仅用标准库 + 可选 pysocks）
# ============================================================

def _http(method: str, url: str, payload=None, token: str = "", timeout: int = 25):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "user-agent": UA,
        "accept": "application/json",
        "apikey": ANON_KEY,
    }
    if token:
        headers["authorization"] = "Bearer " + token
    if body is not None:
        headers["content-type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw.decode("utf-8", "ignore")) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:300]}


def login() -> str:
    """Supabase GoTrue 邮箱密码登录，返回 access_token"""
    code, data = _http(
        "POST",
        f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
        {"email": EMAIL, "password": PASSWORD},
    )
    if code != 200 or not data or not data.get("access_token"):
        raise RuntimeError(f"登录失败 ({code}): {str(data)[:300]}")
    return data["access_token"]


def api_get(path: str, token: str):
    code, data = _http("GET", f"{SUPABASE_URL}/rest/v1/{path}", token=token)
    if code != 200:
        raise RuntimeError(f"GET {path} 失败 ({code}): {str(data)[:300]}")
    return data


def api_patch(path: str, token: str, payload: dict):
    headers_extra = {"prefer": "return=minimal"}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{path}",
        data=body,
        method="PATCH",
        headers={
            "user-agent": UA,
            "accept": "application/json",
            "apikey": ANON_KEY,
            "authorization": "Bearer " + token,
            "content-type": "application/json",
            **headers_extra,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            resp.read()
            return resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        return e.code, raw[:300]


# ============================================================
# Telegram 通知
# ============================================================

def send_tg(lines):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    msg = "\n".join(lines)
    data = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": msg}).encode()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data, method="POST"
        )
        with urllib.request.urlopen(req, timeout=15):
            print("📨 TG 推送成功")
    except Exception as e:
        print(f"⚠️ TG 推送失败：{e}")


# ============================================================
# 主流程
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="EkNodes 自动续期")
    parser.add_argument("--force", action="store_true", help="无条件触发续期（调试用）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的动作，不写数据库")
    args = parser.parse_args()

    dry_run = args.dry_run or os.environ.get("EK_DRY_RUN", "").lower() in ("1", "true", "yes")

    if not EMAIL or not PASSWORD:
        print("❌ 缺少 EK_EMAIL / EK_PASSWORD 环境变量")
        sys.exit(1)

    enable_proxy(PROXY_URL)
    resolve_credentials()

    print("=" * 56)
    print(f"🟢 EkNodes 自动续期  时间: {fmt_dt(now_utc())} (北京时间)")
    if dry_run:
        print("🧪 DRY-RUN 模式：不会写入数据库")
    print("=" * 56)

    # 1. 登录
    token = login()
    print("✅ Supabase 登录成功")

    # 2. 拉取 services / plans / servers
    plans = api_get("plans?select=id,name,renewal_period,price_monthly", token) or []
    plan_map = {p["id"]: p for p in plans}
    services = api_get("services?select=id,name", token) or []
    svc_map = {s["id"]: s.get("name", "?") for s in services}

    servers = api_get(
        "servers?select=id,service_id,plan_id,pterodactyl_id,expires_at&order=created_at.asc",
        token,
    ) or []
    if not servers:
        print("⚠️ 当前账号下没有服务器")
        return

    results = []  # {server, due, renewed, detail}
    for srv in servers:
        name = svc_map.get(srv.get("service_id") or "", "?")
        pid = srv.get("pterodactyl_id")
        name = f"{name} (PID {pid})" if pid else name
        plan = plan_map.get(srv.get("plan_id") or "")
        print(f"── {name} ──")

        if not plan:
            results.append({"server": srv, "renewed": False, "ok": False,
                            "detail": "未找到对应套餐信息"})
            continue

        price = float(plan.get("price_monthly") or 0)
        period = plan.get("renewal_period") or "weekly"
        days = 7 if period == "weekly" else 30

        if price > 0:
            results.append({"server": srv, "renewed": False, "ok": False,
                            "detail": f"付费套餐(price={price:.0f})，自动续期会扣 coins，已跳过"})
            continue

        expires = parse_expiry(srv.get("expires_at", ""))
        remaining_h = (expires - now_utc()).total_seconds() / 3600
        print(f"📅 到期: {fmt_dt(expires)}  |  剩余: {remaining_h:.1f} 小时")

        if not args.force and remaining_h > THRESHOLD_HOURS:
            print(f"⏳ 未到续期窗口（阈值 {THRESHOLD_HOURS:.0f}h），跳过")
            results.append({"server": srv, "renewed": False, "ok": True,
                            "detail": f"剩余 {remaining_h:.1f}h > 阈值 {THRESHOLD_HOURS:.0f}h"})
            continue

        new_expires = now_utc() + datetime.timedelta(days=days)
        if dry_run:
            print(f"🔍 [DRY-RUN] 将续期 → {fmt_dt(new_expires)} (+{days} 天)")
            results.append({"server": srv, "renewed": True, "ok": True, "dry_run": True,
                            "detail": f"DRY-RUN 新到期 {fmt_dt(new_expires)}"})
            continue

        code = api_patch(
            f"servers?id=eq.{urllib.parse.quote(srv['id'])}",
            token,
            {"expires_at": new_expires.isoformat()},
        )
        if code == 204:
            print(f"✅ 续期成功 → {fmt_dt(new_expires)} (+{days} 天)")
            results.append({"server": srv, "renewed": True, "ok": True,
                            "detail": f"新到期 {fmt_dt(new_expires)}"})
        else:
            msg = f"❌ 续期失败: {code}"
            print(msg)
            results.append({"server": srv, "renewed": False, "ok": False, "detail": msg})

    # 3. 汇总 + 通知
    renewed_n = sum(1 for r in results if r.get("renewed") and r.get("ok"))
    print("=" * 56)
    print(f"📊 本次结果：续期成功 {renewed_n} 台 / 共 {len(results)} 台")

    send_tg([
        "🟢 EkNodes 自动续期",
        f"🕐 运行时间: {fmt_dt(now_utc())} (北京时间)",
        "📊 " + " | ".join(
            "✅" if r.get("renewed") and r.get("ok") else ("⏳" if r.get("ok") else "❌")
            for r in results
        ),
        "📝 " + " | ".join(r.get("detail", "") for r in results),
    ])


if __name__ == "__main__":
    try:
        main()
    except (urllib.error.URLError, OSError) as e:
        print(f"\n❌ 网络请求失败: {e}")
        print("   若当前出口被面板/风控拦截，可设置 VLESS_NODE 换个出口网络后重试")
        sys.exit(1)

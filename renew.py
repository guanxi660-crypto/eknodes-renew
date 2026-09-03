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
"""

import os
import sys
import json
import argparse
import datetime
import urllib.request
import urllib.parse
import urllib.error

# ============================================================
# 配置
# ============================================================

SUPABASE_URL = os.environ.get("EK_SUPABASE_URL", "https://pistwrwunlozjyqxjnng.supabase.co").rstrip("/")
# anon key 是公开的（前端 JS 里就有），也可用环境变量覆盖
ANON_KEY = os.environ.get(
    "EK_SUPABASE_ANON",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBpc3R3cnd1bmxvemp5cXhqbm5nIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODc1ODgxNjIsImV4cCI6MjEwMzE2NDE2Mn0.zkj3P5-oK_sP9C9CfRWEJV-wffoLI4x1fM2LqnX1RYA",
).strip()

EMAIL = os.environ.get("EK_EMAIL", "").strip()
PASSWORD = os.environ.get("EK_PASSWORD", "").strip()

# 距到期不足该小时数时才触发续期（默认 48 小时）
_threshold_raw = os.environ.get("EK_RENEW_THRESHOLD_HOURS", "").strip()
THRESHOLD_HOURS = float(_threshold_raw) if _threshold_raw else 48.0
# 可选 Telegram 通知：TG_BOT=chat_id,bot_token
_tg = os.environ.get("TG_BOT", "").split(",")
TG_CHAT_ID = _tg[0].strip() if len(_tg) > 0 else ""
TG_TOKEN = _tg[1].strip() if len(_tg) > 1 else ""

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
# HTTP 工具（仅用标准库）
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
    main()

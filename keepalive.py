#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OptikLink 免費機保活（雙軌）—— 面板層 + dashboard 層。

背景（2026-10-14 實測定案）
--------------------------
OptikLink 個遊戲面板係 Pterodactyl（`control.optiklink.net`），免費機規則見站方 ToS 原文：

    「Login once every 3 days to keep your free servers online … otherwise in
      7 days our system will delete your servers completely.」
    「Logging into the game panel does not count — YOU MUST login to the main
      dashboard.」

即係：**登入遊戲面板唔算數**，一定要登入主 dashboard（`optiklink.net`，Discord OAuth）。
所以本腳本分兩軌：

  軌 1（面板層，零風險）—— Pterodactyl Client API Key
      · GET /api/client/account                     驗 key + 帳號活躍
      · GET /api/client                             伺服器清單
      · GET /api/client/servers/{id}/resources      即時狀態
      · 離線/停機 → POST .../power {"signal":"start"} 自動開返機（可關）

  軌 2（dashboard 層，官方真正要求）—— Discord OAuth
      · POST https://discord.com/api/v9/oauth2/authorize（帶 DISCORD_TOKEN）
      · 攞 response 嘅 location（callback，含 code）
      · 跟 callback 換 optiklink 會話（PHPSESSID）
      · GET https://optiklink.net/ 驗登入態
        陽性標記（實測錄製）："to your Dashboard" ＋（"Logout" 或 "Login to Panel"）
        匿名 / 會 302 去 /home ⇒ 一律當未登入
      · 撞到 Quick Verification（隨機數學題 + Turnstile）→ UNKNOWN，要真瀏覽器／人手

環境變數
--------
  OPTIKLINK_API_KEY    必填。Pterodactyl Client API Key（`ptlc_` 或裸 key 都可以）
  OPTIKLINK_SERVER_ID  選填。唔填就自動攞帳號第一台
  OPTIKLINK_AUTOSTART  選填，預設 "1"。偵測到離線就自動開機；設 "0" 只報告唔動手
  DISCORD_TOKEN        選填。路線 A 嘅 Discord 憑證；冇就只跑軌 1
  PROXY_SERVER         選填。例如 socks5h://127.0.0.1:1080（住宅出口，軌 2 用）
  TG_BOT_TOKEN / TG_CHAT_ID  選填。通知
  DRY_RUN=1            只讀，唔開機、唔提交任何嘢
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

import requests

from renewkit import Outcome, RenewReport, shorten
from renewkit import env
from renewkit import notify
from renewkit.timeutil import now_local

SERVICE = "OptikLink"

PANEL = env.get("OPTIKLINK_PANEL", "https://control.optiklink.net").rstrip("/")
DASH = env.get("OPTIKLINK_DASH", "https://optiklink.net").rstrip("/")
API_KEY = env.get("OPTIKLINK_API_KEY")
SERVER_ID = env.get("OPTIKLINK_SERVER_ID")
AUTOSTART = env.get("OPTIKLINK_AUTOSTART", "1") != "0"
DISCORD_TOKEN = env.get("DISCORD_TOKEN")
PROXY = env.get("PROXY_SERVER") or env.get("HTTPS_PROXY") or ""
DRY_RUN = env.dry_run()
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Discord OAuth（由站方登入流程實測抽出；如果站方換 client_id，
# 可以用 OPTIKLINK_DISCORD_CLIENT_ID 覆蓋，唔使改碼）
DISCORD_CLIENT_ID = env.get("OPTIKLINK_DISCORD_CLIENT_ID", "933437142254887052")
DISCORD_REDIRECT_URI = env.get("OPTIKLINK_DISCORD_REDIRECT", "https://optiklink.com/login")
DISCORD_SCOPE = "guilds guilds.join identify email"

# dashboard 登入態陽性標記（woshizaiyu/optiklink-renew 嘅流量錄製實測）
LOGGED_IN_GROUPS = (("to your Dashboard", "Sincerely provided by"),
                    ("Logout", "Login to Panel"))
SECURITY_MARKS = ("Quick Verification", "verify you are human", "Just a moment",
                  "Checking your browser", "cf-chl")


def _proxies(use: bool = False) -> dict:
    """住宅出口只餵畀 dashboard / Discord 嗰邊。

    面板 API（control.optiklink.net）由機房 IP 直連完全正常（實測 200），
    而且 requests 用 socks5h:// 一定要有 PySocks，否則會拋 InvalidSchema ——
    所以呢兩條路徑分開處理，唔好一刀切。
    """
    if not use or not PROXY:
        return {}
    return {"http": PROXY, "https": PROXY}


def _headers(extra: dict | None = None) -> dict:
    h = {"User-Agent": UA, "Accept": "application/json"}
    if extra:
        h.update(extra)
    return h


# ────────────────────────────── 軌 1：面板層 ──────────────────────────────

def _panel_get(path: str, timeout: int = 25):
    return requests.get(f"{PANEL}{path}",
                        headers=_headers({"Authorization": f"Bearer {API_KEY}"}),
                        timeout=timeout, proxies=_proxies(False))


def track_panel(report: RenewReport) -> str:
    """面板層：驗 key → 攞伺服器 → 睇狀態 → 離線自動開機。回傳 server identifier（可空）。"""
    if not API_KEY:
        report.add("面板", Outcome.FAILED,
                   detail="未配置 OPTIKLINK_API_KEY（Pterodactyl Client API Key）")
        return ""

    try:
        r = _panel_get("/api/client/account")
    except Exception as exc:
        report.add("面板", Outcome.TRANSIENT, detail=f"連唔到面板（{type(exc).__name__}）")
        return ""
    if r.status_code == 401:
        report.add("面板", Outcome.FAILED,
                   detail="API Key 被拒（401）—— 去面板 Account → API Credentials 重發一條")
        return ""
    if r.status_code >= 500:
        report.add("面板", Outcome.TRANSIENT, detail=f"面板 {r.status_code}（上游故障）")
        return ""
    if r.status_code != 200:
        report.add("面板", Outcome.FAILED, detail=f"帳號端點回 HTTP {r.status_code}")
        return ""

    try:
        acc = r.json().get("attributes", {})
    except Exception:
        acc = {}
    who = acc.get("username") or "?"

    try:
        rs = _panel_get("/api/client").json().get("data", [])
    except Exception:
        rs = []
    if not rs:
        report.add(f"{who} · 面板", Outcome.FAILED, detail="帳號下冇任何伺服器")
        return ""

    srv = None
    if SERVER_ID:
        for s in rs:
            if s["attributes"]["identifier"] == SERVER_ID:
                srv = s
                break
    srv = srv or rs[0]
    a = srv["attributes"]
    sid = a["identifier"]
    name = a.get("name") or sid

    state, uptime = "unknown", 0
    try:
        st = _panel_get(f"/api/client/servers/{sid}/resources").json().get("attributes", {})
        state = st.get("current_state") or "unknown"
        uptime = int((st.get("resources") or {}).get("uptime") or 0)
    except Exception:
        pass

    label = f"{who} · {name}"
    if state in ("running", "starting"):
        report.add(label, Outcome.SKIPPED,
                   detail=f"伺服器 {state}（uptime {uptime // 3600}h）· 面板 API 正常")
    elif DRY_RUN:
        report.add(label, Outcome.SKIPPED,
                   detail=f"伺服器 {state} · DRY_RUN 唔開機")
    elif not AUTOSTART:
        report.add(label, Outcome.UNKNOWN,
                   detail=f"伺服器 {state}（AUTOSTART=0，唔動手）")
    else:
        try:
            p = requests.post(f"{PANEL}/api/client/servers/{sid}/power",
                              headers=_headers({"Authorization": f"Bearer {API_KEY}",
                                                "Content-Type": "application/json"}),
                              data=json.dumps({"signal": "start"}),
                              timeout=25, proxies=_proxies(False))
            if p.status_code in (200, 204):
                report.add(label, Outcome.RENEWED, detail=f"伺服器原本 {state} → 已發開機指令")
            else:
                report.add(label, Outcome.UNKNOWN,
                           detail=f"開機指令回 HTTP {p.status_code}（原本 {state}）")
        except Exception as exc:
            report.add(label, Outcome.UNKNOWN,
                       detail=f"開機指令失敗（{type(exc).__name__}）")
    return sid


# ────────────────────────────── 軌 2：dashboard 層 ──────────────────────────────

def _is_logged_in(html: str, url: str) -> bool:
    if "/home" in url or "/login" in url or "/auth" in url:
        return False
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    ok = True
    for group in LOGGED_IN_GROUPS:
        if not any(m in text for m in group):
            ok = False
            break
    return ok


def _security_page(html: str) -> bool:
    return any(m.lower() in html.lower() for m in SECURITY_MARKS)


def _discord_oauth_location(sess: requests.Session) -> str:
    """用 DISCORD_TOKEN 攞 callback location（含 code）。"""
    url = ("https://discord.com/api/v9/oauth2/authorize"
           f"?client_id={DISCORD_CLIENT_ID}&response_type=code"
           f"&scope={urllib.parse.quote(DISCORD_SCOPE)}"
           f"&redirect_uri={urllib.parse.quote(DISCORD_REDIRECT_URI, safe='')}")
    r = sess.post(url,
                  headers={"Authorization": DISCORD_TOKEN,
                           "Content-Type": "application/json",
                           "User-Agent": UA},
                  data=json.dumps({"permissions": "0", "authorize": True}),
                  timeout=25, proxies=_proxies(True))
    if r.status_code != 200:
        raise RuntimeError(f"Discord 授權 HTTP {r.status_code}（token 失效／被風控／要 2FA）")
    loc = (r.json() or {}).get("location")
    if not loc:
        raise RuntimeError("Discord 冇回 callback location")
    return loc


def _browser_verify(cb: str) -> tuple[bool, str]:
    """交畀有頭瀏覽器（xvfb + SeleniumBase UC）過 Quick Verification。"""
    env2 = dict(os.environ)
    env2["OPTIKLINK_CALLBACK"] = cb
    if PROXY:
        env2["PROXY_SERVER"] = PROXY
    try:
        p = subprocess.run([sys.executable, "browser_verify.py"], env=env2,
                           capture_output=True, text=True, timeout=900)
    except Exception as exc:
        return False, f"瀏覽器步驟起唔到（{type(exc).__name__}）"
    out = ((p.stdout or "") + (p.stderr or "")).splitlines()
    for line in out[-25:]:
        print("   [browser]", line[:200])
    if p.returncode == 0:
        return True, "Quick Verification 已過（有頭瀏覽器）· dashboard 已登入"
    if p.returncode == 3:
        return False, "瀏覽器環境缺失（seleniumbase / Chrome）"
    return False, "Quick Verification 未過（Turnstile 令牌或答案被拒）"


def track_dashboard(report: RenewReport) -> None:
    """dashboard 層：Discord OAuth 換會話 → 驗登入態。"""
    if not DISCORD_TOKEN:
        report.add("dashboard", Outcome.UNKNOWN,
                   detail="未配置 DISCORD_TOKEN（路線 A）—— 官方要求嘅登入保活未做")
        return

    sess = requests.Session()
    try:
        loc = _discord_oauth_location(sess)
    except Exception as exc:
        report.add("dashboard", Outcome.FAILED, detail=shorten(str(exc), 110))
        return

    # 站方 redirect_uri 寫住 optiklink.com，但 .com 由機房 IP 會被 CF 403（實測）；
    # .net 同一個 app，所以照樣試 .net 版本。
    candidates = [loc]
    if "optiklink.com" in loc:
        candidates.append(loc.replace("optiklink.com", "optiklink.net"))

    last = ""
    verify_url = ""
    for cb in candidates:
        try:
            c = sess.get(cb, headers={"User-Agent": UA}, timeout=30,
                         allow_redirects=True, proxies=_proxies(True))
            last = f"{c.status_code} {c.url}"
            if _security_page(c.text):
                # 回跳後站方先出 Quick Verification（隨機數學題 + 真 Turnstile）：
                # 純 HTTP 造唔出令牌，要交畀有頭瀏覽器（見 browser_verify.py）
                verify_url = cb
                break
            if "PHPSESSID" in sess.cookies.get_dict():
                break
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"

    if verify_url:
        ok, detail = _browser_verify(verify_url)
        report.add("dashboard", Outcome.RENEWED if ok else Outcome.UNKNOWN, detail=detail)
        return

    try:
        d = sess.get(f"{DASH}/", headers={"User-Agent": UA, "Accept": "text/html"},
                     timeout=30, allow_redirects=True, proxies=_proxies(True))
    except Exception as exc:
        report.add("dashboard", Outcome.TRANSIENT,
                   detail=f"攞唔到 dashboard（{type(exc).__name__}）")
        return

    if _security_page(d.text):
        report.add("dashboard", Outcome.UNKNOWN,
                   detail="dashboard 撞 Quick Verification／CF 挑戰 —— 需要真瀏覽器／人手")
        return
    if _is_logged_in(d.text, str(d.url)):
        user = ""
        m = re.search(r"([A-Za-z0-9_.]{2,32}#\d+)", d.text)
        if m:
            user = f"（{m.group(1)}）"
        report.add(f"dashboard{user}", Outcome.RENEWED,
                   detail=f"Discord OAuth 登入成功，已刷新保活（{now_local()}）")
    else:
        report.add("dashboard", Outcome.FAILED,
                   detail=f"未見登入態（最後跳轉 {shorten(last, 60)}）—— token 可能失效或站方改版")


# ────────────────────────────── 主流程 ──────────────────────────────

def _proxy_ready() -> bool:
    """SOCKS 代理要 PySocks 先得；冇就自動降級直連（唔好因為裝唔到就整個 job 炸）。"""
    global PROXY
    if not PROXY:
        return False
    try:
        import socks  # noqa: F401
        return True
    except Exception:
        print("  ⚠️ 有 PROXY_SERVER 但冇 PySocks，降級直連（pip install pysocks 可解）")
        PROXY = ""
        return False


def main() -> int:
    print("#" * 46)
    print("   OptikLink 保活（面板層 + dashboard 層）")
    print("#" * 46)
    _proxy_ready()
    print(f"  面板 {PANEL} ｜ dashboard {DASH} ｜ 代理 {'有' if PROXY else '冇'}"
          f" ｜ DRY_RUN={DRY_RUN}")

    report = RenewReport(SERVICE)
    report.renderer = render_tg

    track_panel(report)
    track_dashboard(report)

    return report.finish(notify_tg=True)


def render_tg(report: RenewReport) -> str:
    """精簡通知：表頭一行 + 每項一行（同 fleet 其他 repo 一致）。"""
    head = f"🎮 OptikLink ｜ {now_local()} ｜ {report.worst.icon} {_status_word(report)}"
    lines = []
    for r in report.results:
        d = f" · {r.detail}" if r.detail else ""
        lines.append(f"{r.outcome.icon} {r.name}{d}")
    body = "\n".join(lines) if lines else "（冇結果）"
    tail = ""
    if any(x.outcome is Outcome.FAILED for x in report.results):
        tail = "\n⚠️ 需要你處理（睇上面行）"
    return f"{head}\n{body}{tail}"


def _status_word(report: RenewReport) -> str:
    return {Outcome.RENEWED: "已保活", Outcome.SKIPPED: "狀態正常",
            Outcome.ALREADY_MAX: "無需操作", Outcome.UNKNOWN: "要留意",
            Outcome.TRANSIENT: "上游故障", Outcome.FAILED: "需要處理"}[report.worst]


if __name__ == "__main__":
    raise SystemExit(main())

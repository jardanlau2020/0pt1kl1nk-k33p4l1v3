#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OptikLink 免費機保活（雙軌 · 多帳號）—— OptikLink01 / OptikLink02 …

背景（2026-10-14 實測定案）
--------------------------
OptikLink 個遊戲面板係 Pterodactyl（`control.optiklink.net`），免費機規則見站方 ToS 原文：

    「Login once every 3 days to keep your free servers online … otherwise in
      7 days our system will delete your servers completely.」
    「Logging into the game panel does not count — YOU MUST login to the main
      dashboard.」

即係：**登入遊戲面板唔算數**，一定要登入主 dashboard（`optiklink.net`，Discord OAuth）。
所以每個帳號都跑兩軌：

  軌 1（面板層，零風險）—— Pterodactyl Client API Key
      · GET /api/client/account                     驗 key + 帳號活躍
      · GET /api/client                             伺服器清單
      · GET /api/client/servers/{id}/resources      即時狀態
      · 離線/停機 → POST .../power {"signal":"start"} 自動開返機（可關）

  軌 2（dashboard 層，官方真正要求）—— Discord OAuth（每個帳號自己一條 token）
      · POST https://discord.com/api/v9/oauth2/authorize
      · 攞 callback location → 交畀有頭瀏覽器（見 browser_verify.py）
      · 站方會出 Quick Verification（隨機數學題 + 真 Turnstile）→ 必須真·有頭瀏覽器
      · 過完開 https://optiklink.net/ 驗登入態

多帳號環境變數（每個帳號一組，PREFIX = OPTIKLINK01 / OPTIKLINK02 …）
------------------------------------------------------------------
  {PREFIX}_API_KEY          必填。Pterodactyl Client API Key（`ptlc_` 或裸 key）
  {PREFIX}_SERVER_ID        選填。唔填自動攞該帳號第一台
  {PREFIX}_DISCORD_TOKEN    選填。冇就只跑軌 1（會報 UNKNOWN 提醒）
  **兼容舊名**：OptikLink01 如果冇 `OPTIKLINK01_*`，會退回用 `OPTIKLINK_API_KEY` /
  `OPTIKLINK_SERVER_ID` / `DISCORD_TOKEN`（2026-10-15 之前嘅命名），唔使重貼憑據。

共通環境變數
------------
  OPTIKLINK_AUTOSTART  選填，預設 "1"。偵測到離線就自動開機；"0" 只報告唔動手
  PROXY_SERVER         選填。socks5h://…（住宅出口；optiklink.com 對機房 IP 回 CF 403）
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
from renewkit.timeutil import now_local

SERVICE = "OptikLink"

PANEL = env.get("OPTIKLINK_PANEL", "https://control.optiklink.net").rstrip("/")
DASH = env.get("OPTIKLINK_DASH", "https://optiklink.net").rstrip("/")
AUTOSTART = env.get("OPTIKLINK_AUTOSTART", "1") != "0"
PROXY = env.get("PROXY_SERVER") or env.get("HTTPS_PROXY") or ""
DRY_RUN = env.dry_run()
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Discord OAuth（由站方登入流程實測抽出；站方換 client_id 可用
# OPTIKLINK_DISCORD_CLIENT_ID 覆蓋，唔使改碼）
DISCORD_CLIENT_ID = env.get("OPTIKLINK_DISCORD_CLIENT_ID", "933437142254887052")
DISCORD_REDIRECT_URI = env.get("OPTIKLINK_DISCORD_REDIRECT", "https://optiklink.com/login")
DISCORD_SCOPE = "guilds guilds.join identify email"

# dashboard 登入態陽性標記（第三方流量錄製實測：匿名 / 會 302 去 /home）
LOGGED_IN_GROUPS = (("to your Dashboard", "Sincerely provided by"),
                    ("Logout", "Login to Panel"))
SECURITY_MARKS = ("Quick Verification", "verify you are human", "Just a moment",
                  "Checking your browser", "cf-chl")

# 帳號順序 + 舊名兼容（2026-10-15 之前只有一個帳號，用咗通用名）
ACCOUNT_PREFIXES = (("OptikLink01", "OPTIKLINK01"), ("OptikLink02", "OPTIKLINK02"))


def _accounts() -> list[dict]:
    out: list[dict] = []
    for label, prefix in ACCOUNT_PREFIXES:
        acc = {"label": label,
               "key": env.get(f"{prefix}_API_KEY"),
               "sid": env.get(f"{prefix}_SERVER_ID"),
               "dtok": env.get(f"{prefix}_DISCORD_TOKEN")}
        if label == "OptikLink01" and not acc["key"]:
            acc["key"] = env.get("OPTIKLINK_API_KEY")
            acc["sid"] = acc["sid"] or env.get("OPTIKLINK_SERVER_ID")
            acc["dtok"] = acc["dtok"] or env.get("DISCORD_TOKEN")
        if acc["key"] or acc["dtok"]:
            out.append(acc)
    return out


def _proxies(use: bool = False) -> dict:
    """住宅出口只餵畀 dashboard / Discord 嗰邊。

    面板 API（control.optiklink.net）由機房 IP 直連完全正常（實測 200），
    而且 requests 用 socks5h:// 一定要有 PySocks，否則拋 InvalidSchema ——
    所以兩條路徑分開處理，唔好一刀切。
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

def _panel_get(path: str, key: str, timeout: int = 25):
    return requests.get(f"{PANEL}{path}",
                        headers=_headers({"Authorization": f"Bearer {key}"}),
                        timeout=timeout, proxies=_proxies(False))


def track_panel(report: RenewReport, acc: dict) -> str:
    """面板層：驗 key → 攞伺服器 → 睇狀態 → 離線自動開機。回傳 server identifier（可空）。"""
    label = acc["label"]
    if not acc["key"]:
        report.add(f"{label} · 面板", Outcome.FAILED,
                   detail="未配置 API Key（面板 Account → API Credentials）")
        return ""

    try:
        r = _panel_get("/api/client/account", acc["key"])
    except Exception as exc:
        report.add(f"{label} · 面板", Outcome.TRANSIENT,
                   detail=f"連唔到面板（{type(exc).__name__}）")
        return ""
    if r.status_code == 401:
        report.add(f"{label} · 面板", Outcome.FAILED,
                   detail="API Key 被拒（401）—— 去面板 Account → API Credentials 重發一條")
        return ""
    if r.status_code >= 500:
        report.add(f"{label} · 面板", Outcome.TRANSIENT,
                   detail=f"面板 {r.status_code}（上游故障）")
        return ""
    if r.status_code != 200:
        report.add(f"{label} · 面板", Outcome.FAILED, detail=f"帳號端點回 HTTP {r.status_code}")
        return ""

    try:
        acc_info = r.json().get("attributes", {})
    except Exception:
        acc_info = {}
    who = acc_info.get("username") or "?"

    try:
        rs = _panel_get("/api/client", acc["key"]).json().get("data", [])
    except Exception:
        rs = []
    if not rs:
        report.add(f"{label} · 面板", Outcome.FAILED, detail=f"{who}：帳號下冇任何伺服器")
        return ""

    srv = None
    if acc["sid"]:
        for s in rs:
            if s["attributes"]["identifier"] == acc["sid"]:
                srv = s
                break
    srv = srv or rs[0]
    a = srv["attributes"]
    sid = a["identifier"]
    name = a.get("name") or sid

    state, uptime = "unknown", 0
    try:
        st = _panel_get(f"/api/client/servers/{sid}/resources", acc["key"]).json().get("attributes", {})
        state = st.get("current_state") or "unknown"
        uptime = int((st.get("resources") or {}).get("uptime") or 0)
    except Exception:
        pass

    row = f"{label} · {name}"
    if state in ("running", "starting"):
        report.add(row, Outcome.SKIPPED,
                   detail=f"伺服器 {state}（uptime {uptime // 3_600_000}h）· 面板 API 正常（{who}）")
    elif DRY_RUN:
        report.add(row, Outcome.SKIPPED, detail=f"伺服器 {state} · DRY_RUN 唔開機")
    elif not AUTOSTART:
        report.add(row, Outcome.UNKNOWN, detail=f"伺服器 {state}（AUTOSTART=0，唔動手）")
    else:
        try:
            p = requests.post(f"{PANEL}/api/client/servers/{sid}/power",
                              headers=_headers({"Authorization": f"Bearer {acc['key']}",
                                                "Content-Type": "application/json"}),
                              data=json.dumps({"signal": "start"}),
                              timeout=25, proxies=_proxies(False))
            if p.status_code in (200, 204):
                report.add(row, Outcome.RENEWED, detail=f"伺服器原本 {state} → 已發開機指令")
            else:
                report.add(row, Outcome.UNKNOWN,
                           detail=f"開機指令回 HTTP {p.status_code}（原本 {state}）")
        except Exception as exc:
            report.add(row, Outcome.UNKNOWN, detail=f"開機指令失敗（{type(exc).__name__}）")
    return sid


# ────────────────────────────── 軌 2：dashboard 層 ──────────────────────────────

def _is_logged_in(html: str, url: str) -> bool:
    if "/home" in url or "/login" in url or "/auth" in url:
        return False
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return all(any(m in text for m in g) for g in LOGGED_IN_GROUPS)


def _security_page(html: str) -> bool:
    return any(m.lower() in html.lower() for m in SECURITY_MARKS)


def _discord_oauth_location(dtok: str) -> str:
    """用該帳號嘅 DISCORD_TOKEN 攞 callback location（含 code）。

    2026-10-15 修正：Discord API **唔應該行住宅代理** —— discord.com 由機房 IP
    直連冇問題，反而住宅節點係網友共享、隨時掉線（run 38701173992 就係咁死：
    `SOCKSHTTPSConnectionPool(host='discord.com') Max retries exceeded`）。
    所以改為**直連優先**，直連失敗才退回代理。
    """
    url = ("https://discord.com/api/v9/oauth2/authorize"
           f"?client_id={DISCORD_CLIENT_ID}&response_type=code"
           f"&scope={urllib.parse.quote(DISCORD_SCOPE)}"
           f"&redirect_uri={urllib.parse.quote(DISCORD_REDIRECT_URI, safe='')}")
    payload = json.dumps({"permissions": "0", "authorize": True})
    headers = {"Authorization": dtok, "Content-Type": "application/json", "User-Agent": UA}
    last = ""
    for use_proxy in (False, True):
        if use_proxy and not PROXY:
            break
        try:
            r = requests.post(url, headers=headers, data=payload, timeout=25,
                              proxies=_proxies(use_proxy))
        except Exception as exc:
            last = f"{type(exc).__name__}（{'經代理' if use_proxy else '直連'}）"
            continue
        if r.status_code == 200:
            loc = (r.json() or {}).get("location")
            if loc:
                return loc
            last = "Discord 冇回 callback location"
            continue
        if r.status_code in (401, 403):
            raise RuntimeError(f"Discord 授權 HTTP {r.status_code}"
                               "（token 失效／被風控／要 2FA）—— 要人手換 token")
        last = f"HTTP {r.status_code}"
    raise RuntimeError(f"Discord 授權失敗：{last}")


# ── 住宅出口健康檢查 / 換節點（節點係網友共享，掉線係常態）───────────────
MIHOMO_API = env.get("MIHOMO_API", "http://127.0.0.1:9091")


def _proxy_alive(timeout: int = 12) -> bool:
    if not PROXY:
        return False
    try:
        r = requests.get("https://optiklink.com/", headers={"User-Agent": UA},
                         timeout=timeout, proxies=_proxies(True), allow_redirects=False)
        return r.status_code < 500
    except Exception:
        return False


def _repick_node(tries: int = 8) -> bool:
    """經 mihomo API 換另一條家宽落地（KR/TH 優先）。"""
    try:
        d = requests.get(f"{MIHOMO_API}/proxies", timeout=15).json()["proxies"]
    except Exception as exc:
        print(f"  ⚠️ 攞唔到 mihomo 節點清單（{type(exc).__name__}）")
        return False
    home = [k for k in d if "家宽" in k]
    prio = ("KR", "TH", "VN", "TW", "HK", "SG")

    def rank(n):
        for i, c in enumerate(prio):
            if c + "-家宽" in n:
                return i
        return len(prio)

    for n in sorted(home, key=rank)[:tries]:
        try:
            requests.put(f"{MIHOMO_API}/proxies/GLOBAL", json={"name": n}, timeout=15)
        except Exception:
            continue
        time.sleep(3)
        if _proxy_alive():
            print(f"  🔄 已換住宅落地：{n}")
            return True
    print("  ✗ 換咗幾條節點都唔通 optiklink")
    return False


def _browser_verify(cb: str) -> tuple[bool, str]:
    """交畀有頭瀏覽器（xvfb + SeleniumBase UC）過 Quick Verification。"""
    env2 = dict(os.environ)
    env2["PYTHONUNBUFFERED"] = "1"
    env2["OPTIKLINK_CALLBACK"] = cb
    if PROXY:
        env2["PROXY_SERVER"] = PROXY
    try:
        p = subprocess.run([sys.executable, "browser_verify.py"], env=env2,
                           capture_output=True, text=True, timeout=900)
    except Exception as exc:
        return False, f"瀏覽器步驟起唔到（{type(exc).__name__}）"
    out = ((p.stdout or "") + (p.stderr or "")).splitlines()
    head, tail = out[:30], out[-30:]
    for line in head:
        print("   [browser|頭]", line[:200])
    if len(out) > 60:
        print(f"   [browser] …（中間省略 {len(out) - 60} 行）…")
    for line in tail:
        print("   [browser|尾]", line[:200])
    if p.returncode == 0:
        return True, "Quick Verification 已過（有頭瀏覽器）· dashboard 已登入"
    if p.returncode == 4:
        return False, "住宅出口斷線（瀏覽器 ERR_CONNECTION_CLOSED）—— 環境問題，等下次"
    if p.returncode == 3:
        return False, "瀏覽器環境缺失（seleniumbase / Chrome）"
    return False, "Quick Verification 未過（Turnstile 令牌或答案被拒）"


def _dashboard_once(acc: dict) -> tuple[bool, str, bool]:
    """跑一次 dashboard 保活。回傳 (成功, 說明, 係唔係環境性失敗)。

    環境性失敗（住宅出口斷線、Discord 連唔到）＝transient，唔應該標紅叫人處理；
    認證性失敗（token 失效、驗證被拒）＝要人手，標紅。
    """
    if not acc["dtok"]:
        return False, "未配置 DISCORD_TOKEN（路線 A）—— 官方要求嘅登入保活未做", False

    try:
        loc = _discord_oauth_location(acc["dtok"])
    except Exception as exc:
        msg = str(exc)
        transient = ("HTTP" not in msg)          # 401/403 係 token 問題，其餘當網絡
        return False, shorten(msg, 110), transient

    # 站方 redirect_uri 寫住 optiklink.com，但 .com 由機房 IP 會被 CF 403（實測）；
    # .net 同一個 app，所以照樣試 .net 版本。
    candidates = [loc]
    if "optiklink.com" in loc:
        candidates.append(loc.replace("optiklink.com", "optiklink.net"))

    sess = requests.Session()
    last = ""
    verify_url = ""
    for cb in candidates:
        try:
            c = sess.get(cb, headers={"User-Agent": UA}, timeout=30,
                         allow_redirects=True, proxies=_proxies(True))
            last = f"{c.status_code} {c.url}"
            if _security_page(c.text):
                verify_url = cb
                break
            if "PHPSESSID" in sess.cookies.get_dict():
                break
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"

    if verify_url:
        # ⚠️ OAuth code 係一次性：上面 HTTP 嗰次已經用咗，瀏覽器再開同一條 URL
        # 只會見到錯誤／空白頁（run 38672674414 就係咁卡到 chromedriver 逾時）。
        fresh = verify_url
        try:
            fresh = _discord_oauth_location(acc["dtok"])
        except Exception as exc:
            print(f"  ⚠️ 重新授權失敗（{type(exc).__name__}），用返舊 callback 試")
        ok, detail = _browser_verify(fresh)
        transient = "住宅出口斷線" in detail or "環境" in detail
        return ok, detail, transient

    try:
        d = sess.get(f"{DASH}/", headers={"User-Agent": UA, "Accept": "text/html"},
                     timeout=30, allow_redirects=True, proxies=_proxies(True))
    except Exception as exc:
        return False, f"攞唔到 dashboard（{type(exc).__name__}）", True

    if _security_page(d.text):
        return False, "dashboard 撞 Quick Verification／CF 挑戰 —— 需要真瀏覽器／人手", False
    if _is_logged_in(d.text, str(d.url)):
        user = ""
        m = re.search(r"([A-Za-z0-9_.]{2,32}#\d+)", d.text)
        if m:
            user = f"（{m.group(1)}）"
        return True, f"Discord OAuth 登入成功，已刷新保活（{now_local()}）{user}", False
    return False, f"未見登入態（最後跳轉 {shorten(last, 60)}）—— token 可能失效或站方改版", False


def track_dashboard(report: RenewReport, acc: dict) -> None:
    """dashboard 層：Discord OAuth → 有需要就用有頭瀏覽器過 Quick Verification。

    2026-10-15 加：跑之前先驗住宅出口，唔通就換節點；環境性失敗自動再試一次
    （run 38701173992 兩個帳號都係死喺住宅節點中途掉線）。
    """
    label = acc["label"]
    row = f"{label} · dashboard"
    if not acc["dtok"]:
        report.add(row, Outcome.UNKNOWN,
                   detail="未配置 DISCORD_TOKEN（路線 A）—— 官方要求嘅登入保活未做")
        return

    if PROXY and not _proxy_alive():
        print("  ⚠️ 住宅出口唔通 optiklink，先換節點…")
        _repick_node()

    ok, detail, transient = _dashboard_once(acc)
    if not ok and transient:
        print(f"  ↻ 第一次失敗（{detail}）→ 檢查／換住宅節點再試一次")
        if PROXY and not _proxy_alive():
            _repick_node()
        ok, detail, transient = _dashboard_once(acc)

    if ok:
        report.add(row, Outcome.RENEWED, detail=detail)
    elif transient:
        report.add(row, Outcome.TRANSIENT, detail=detail)
    else:
        report.add(row, Outcome.FAILED, detail=detail)


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
    print("   OptikLink 保活（面板層 + dashboard 層 · 多帳號）")
    print("#" * 46)
    _proxy_ready()
    accounts = _accounts()
    print(f"  面板 {PANEL} ｜ dashboard {DASH} ｜ 代理 {'有' if PROXY else '冇'}"
          f" ｜ DRY_RUN={DRY_RUN} ｜ 帳號 {len(accounts)} 個"
          f"：{', '.join(a['label'] for a in accounts) or '（冇）'}")

    report = RenewReport(SERVICE)
    report.renderer = render_tg

    if not accounts:
        report.add("配置", Outcome.FAILED,
                   detail="冇任何帳號憑據（設 OPTIKLINK01_API_KEY / OPTIKLINK02_API_KEY …）")
        return report.finish(notify_tg=True)

    for acc in accounts:
        print(f"\n── {acc['label']} ──")
        track_panel(report, acc)
        track_dashboard(report, acc)

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

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OptikLink dashboard 登入：用真·有頭瀏覽器過 Quick Verification。

為咩要另開一支瀏覽器
--------------------
`optiklink.net/login?code=…` 回跳之後，站方會先出一個 **Quick Verification** 頁：
**隨機數學題 ＋ 真 Cloudflare Turnstile（managed 模式，服務端驗 cf-turnstile-response）**。
純 HTTP 造唔出 Turnstile 令牌；無頭瀏覽器一樣被攔（指紋），**必須真·有頭瀏覽器 +
OS 級滑鼠點擊**（SeleniumBase UC 模式嘅 uc_gui_click_captcha）。所以呢步：
    有頭 Chromium（xvfb 之下）→ 開 callback → 讀數學題 → 填答案 →
    點 Turnstile → 等令牌 → 提交 → 開 dashboard 驗登入態。

環境變數
--------
  OPTIKLINK_CALLBACK   必填。`keepalive.py` 用 Discord token 換到嘅 callback URL（含 code）
  PROXY_SERVER         選填。socks5://…（住宅出口；optiklink.com 對機房 IP 回 CF 403）
  OPTIKLINK_DASH       選填，預設 https://optiklink.net
  DRY_RUN=1            只開頁睇有冇驗證頁，唔提交

退出碼：0 = dashboard 已登入；2 = 過唔到驗證／未登入；3 = 環境問題（缺 seleniumbase 等）
"""
from __future__ import annotations

import os
import re
import sys
import time

DASH = (os.environ.get("OPTIKLINK_DASH") or "https://optiklink.net").rstrip("/")
CALLBACK = (os.environ.get("OPTIKLINK_CALLBACK") or "").strip()
PROXY = (os.environ.get("PROXY_SERVER") or "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").strip() not in ("", "0", "false", "False")

MATH_STRONG_RE = re.compile(r"<strong[^>]*>([^<]*)</strong>", re.I)
MATH_TEXT_RE = re.compile(r"What\s+is\s+([0-9+\-×*/÷\s]+?)\s*\?", re.I)
MATH_TOKEN_RE = re.compile(r"\d+|[+\-×*/÷]")
TURNSTILE_JS = ('var e=document.querySelector(\'input[name="cf-turnstile-response"]\');'
                'return e ? String(e.value) : "";')
MATH_JS = ('var e=document.querySelector(\'input[name="math_answer"]\');'
           'return e ? String(e.value) : "";')
TURNSTILE_MAX_RETRY = 5
POLL_STEP = 4

# 登入態陽性標記（同 keepalive.py 一致）
LOGGED_IN_GROUPS = (("to your Dashboard", "Sincerely provided by"),
                    ("Logout", "Login to Panel"))


def _mask(u: str) -> str:
    return u.split("?")[0] + "?code=…" if "?" in u else u


def solve_math(a, op, b):
    a, b = int(a), int(b)
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op in ("*", "×"):
        return a * b
    if op in ("/", "÷"):
        return a // b
    raise ValueError(f"未知運算子 {op}")


def read_math(html: str) -> str:
    m = MATH_STRONG_RE.search(html) or MATH_TEXT_RE.search(re.sub(r"<[^>]+>", " ", html))
    if not m:
        raise RuntimeError("驗證頁搵唔到數學題")
    return re.sub(r"\s+", " ", m.group(1)).strip()


def solve_expr(expr: str) -> int:
    """左到右求值（實測題面係多操作數鏈，例如 "2 + 2 + 1"）。"""
    tokens = MATH_TOKEN_RE.findall(expr)
    if len(tokens) < 3 or not tokens[0].isdigit():
        raise ValueError(f"算式不完整: {expr!r}")
    acc = int(tokens[0])
    i = 1
    while i < len(tokens):
        if tokens[i].isdigit() or i + 1 >= len(tokens) or not tokens[i + 1].isdigit():
            raise ValueError(f"算式不完整: {expr!r}")
        acc = solve_math(acc, tokens[i], int(tokens[i + 1]))
        i += 2
    return acc


def set_math(sb, answer: int) -> None:
    want = str(answer)
    sb.update_text('input[name="math_answer"]', want)
    got = sb.execute_script(MATH_JS)
    if got != want:                      # update_text 偶爾填唔入（表面睇唔出）
        sb.execute_script('var e=document.querySelector(\'input[name="math_answer"]\');'
                          f'e.value="{want}";'
                          'e.dispatchEvent(new Event("input",{bubbles:true}));')
        time.sleep(1)
        got = sb.execute_script(MATH_JS)
    print(f"    答案讀回: {got!r}")
    if got != want:
        raise RuntimeError(f"數學答案填唔入（期望 {want}，實際 {got!r}）")


def is_logged_in(html: str) -> bool:
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return all(any(m in text for m in g) for g in LOGGED_IN_GROUPS)


def main() -> int:
    if not CALLBACK:
        print("❌ 冇 OPTIKLINK_CALLBACK（keepalive.py 應該傳入）")
        return 3
    try:
        from seleniumbase import SB
    except Exception as exc:
        print(f"❌ 缺 seleniumbase（{exc}）")
        return 3

    kwargs = {"uc": True, "test": True, "locale": "en", "raise_test_failure": True}
    if PROXY:
        # Chrome 認 socks5://（唔認 socks5h://）
        kwargs["proxy"] = PROXY.replace("socks5h://", "socks5://")
        print(f"  代理: {kwargs['proxy']}")
    print(f"  開 callback: {_mask(CALLBACK)}")

    with SB(**kwargs) as sb:
        print("  [1] 瀏覽器起好，開 callback …", flush=True)
        try:
            sb.set_page_load_timeout(180)
        except Exception:
            pass
        try:
            sb.uc_open_with_reconnect(CALLBACK, reconnect_time=4.0)
        except Exception as exc:
            # UC 模式開頁偶爾卡死（住宅代理慢／reconnect 問題）→ 退普通開頁
            print(f"  ⚠️ uc_open_with_reconnect 失敗（{type(exc).__name__}），改用 sb.open", flush=True)
            sb.open(CALLBACK)
        time.sleep(3)
        print(f"  [2] 已到頁：{_mask(sb.get_current_url() or '')}", flush=True)
        has_math = sb.is_element_present('input[name="math_answer"]')
        print(f"  [3] 有冇驗證頁（math_answer）：{has_math}", flush=True)

        for attempt in range(1, 4):
            if not sb.is_element_present('input[name="math_answer"]'):
                break
            print(f"  Quick Verification（第 {attempt} 次）...", flush=True)
            expr = read_math(sb.get_page_source())
            answer = solve_expr(expr)
            print(f"    題目 {expr} = {answer}")
            if DRY_RUN:
                print("    DRY_RUN：唔提交")
                break
            set_math(sb, answer)
            sb.execute_script("document.querySelector('.cf-turnstile')"
                              ".scrollIntoView({block:'center'})")
            time.sleep(1)

            token = ""
            for t in range(1, TURNSTILE_MAX_RETRY + 1):
                sb.uc_gui_click_captcha()
                for _ in range(10):
                    time.sleep(POLL_STEP)
                    token = sb.execute_script(TURNSTILE_JS)
                    if len(token) > 50:
                        break
                if len(token) > 50:
                    break
                print(f"    ⚠️ Turnstile 未放行（第 {t}/{TURNSTILE_MAX_RETRY} 次），再點…")
            if len(token) <= 50:
                print("❌ Turnstile 令牌未拿到")
                return 2
            print(f"    ✅ Turnstile 令牌 {len(token)} 字，提交…")
            sb.click('button[type="submit"]')
            time.sleep(3)

        url = sb.get_current_url() or ""
        print(f"    驗證後 URL: {_mask(url)}")
        if "/error/vpn" in url:
            print("❌ 站方 VPN 攔截頁（出口 IP 被拒）")
            return 2
        if "/error/" in url:
            print(f"❌ 登入失敗頁: {url.split('?')[0]}")
            return 2

        sb.open(f"{DASH}/")
        sb.wait_for_ready_state_complete()
        time.sleep(2)
        html = sb.get_page_source() or ""
        if is_logged_in(html):
            print("✅ dashboard 已登入（保活完成）")
            return 0
        print(f"❌ 未見登入態（最終 URL: {_mask(sb.get_current_url() or '')}）", flush=True)
        try:
            sb.save_screenshot("dashboard_fail.png")
        except Exception:
            pass
        try:
            with open("dashboard_fail.html", "w", encoding="utf-8") as f:
                f.write(sb.get_page_source() or "")
            print("  已寫出 dashboard_fail.html")
        except Exception:
            pass
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

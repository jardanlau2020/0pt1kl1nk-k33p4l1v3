# OptikLink 保活（面板層 + dashboard 層）

跑喺 GitHub Actions，每 **2 日**一次（站方要求每 3 日登入一次，我哋留緩衝）。

## 站方規則（ToS 原文）

> Login once every 3 days to keep your free servers online … otherwise in 7 days
> our system will delete your servers completely.
> **Logging into the game panel does not count — YOU MUST login to the main dashboard.**

即係：**登入遊戲面板唔算數**，要登入主 dashboard（`optiklink.net`，Discord OAuth）。

## 兩軌做啲乜

| 軌 | 機制 | 憑據 | 備註 |
|---|---|---|---|
| 1. 面板層 | Pterodactyl Client API：`/api/client/account`、`/api/client`、`/resources`；離線自動 `power start` | `OPTIKLINK_API_KEY` | 零風險，機房 IP 都通 |
| 2. dashboard 層 | Discord OAuth（`discord.com/api/v9/oauth2/authorize`）換 `optiklink` 會話 → 驗登入態 | `DISCORD_TOKEN` | 站方真正要求嘅「登入保活」；`optiklink.com` 對機房 IP 回 CF 403，故走住宅出口 |

## Secrets

| Secret | 說明 |
|---|---|
| `OPTIKLINK_API_KEY` | 面板 Account → API Credentials 發嘅 Client API Key（`ptlc_` 或裸 key） |
| `OPTIKLINK_SERVER_ID` | 選填，唔填自動攞第一台 |
| `DISCORD_TOKEN` | 路線 A：Discord 憑證（等同密碼，**唔好外流**） |
| `SUB_URL` | 選填：住宅出口訂閱（mihomo/Clash YAML）；冇就直連（軌 2 可能被 CF 擋） |
| `TG_BOT_TOKEN` / `TG_CHAT_ID` | 選填：通知 |

## 手動跑

Actions → OptikLink Keepalive → Run workflow（可揀 `dry_run` 只讀）。

## 注意

- `OPTIKLINK_AUTOSTART=0` 可以關掉「離線自動開機」（只報告）。
- 撞到站方 **Quick Verification（隨機數學題 + Turnstile）** 時會報 UNKNOWN 並叫你人手撳一下。

#!/usr/bin/env bash
# setup_proxy.sh —— 住宅出口（cfnew 家宽訂閱 / mihomo）
#
# 2026-10-14 新建。背景：OptikLink 個 dashboard（optiklink.com）由機房 IP 直連會被
# Cloudflare 403「Just a moment...」，實測**住宅出口（KR / TH 家宽）就通**
# （301 → 正常 colo）。呢個腳本負責：
#   1. 落 mihomo（v1.19.26 —— v1.19.25 會報 openvpn tls-crypt unset 起唔到）
#   2. 落 SUB_URL 訂閱（cfnew 家宽 = CF 前置 → OpenVPN 住宅落地；sing-box 唔支援 OpenVPN）
#   3. 逐條試「家宽」落地，揀第一條打得通 optiklink.com 嘅（唔使登入都測得到）
#   4. 成功就寫 PROXY_SERVER=socks5h://127.0.0.1:1080 落 $GITHUB_ENV
#
# 失敗唔緊要：只會令 dashboard 軌冇住宅出口，面板軌（Pterodactyl API）照跑。
set -uo pipefail

MIHOMO_VER="${MIHOMO_VER:-v1.19.26}"
MIHOMO_TRIES="${MIHOMO_TRIES:-25}"
SUB="${SUB_URL:-}"
[ -z "$SUB" ] && { echo "[proxy] 冇設 SUB_URL，跳過（軌 1 照跑）"; exit 0; }

echo "[proxy] 下載 mihomo ${MIHOMO_VER} ..."
curl -sL --max-time 120 -o /tmp/mihomo.gz \
  "https://github.com/MetaCubeX/mihomo/releases/download/${MIHOMO_VER}/mihomo-linux-amd64-${MIHOMO_VER}.gz" \
  && gzip -dc /tmp/mihomo.gz > /tmp/mihomo 2>/dev/null && chmod +x /tmp/mihomo \
  || { echo "[proxy] ✗ mihomo 下載失敗"; exit 0; }
echo "[proxy] ✅ $(/tmp/mihomo -v 2>&1 | head -1)"

echo "[proxy] 下載訂閱 ..."
curl -s --max-time 60 -H "user-agent: v2rayNG/1.8.5" "$SUB" -o /tmp/sub.yaml || true
[ -s /tmp/sub.yaml ] || { echo "[proxy] ✗ 訂閱下載失敗"; exit 0; }

sed -i 's/^mixed-port: .*/mixed-port: 1080/; s/^external-controller: .*/external-controller: 127.0.0.1:9091/; s/^mode: .*/mode: global/' /tmp/sub.yaml

mkdir -p /tmp/mihomo_data
nohup /tmp/mihomo -f /tmp/sub.yaml -d /tmp/mihomo_data > /tmp/mihomo.log 2>&1 &
sleep 18
if ! curl -s --max-time 10 http://127.0.0.1:9091/version >/dev/null 2>&1; then
  echo "[proxy] ✗ mihomo 起唔到，log 尾："; tail -3 /tmp/mihomo.log; exit 0
fi
echo "[proxy] ✅ mihomo 起動（mode=global, mixed-port 1080）"

# 家宽落地清單（按實測過嘅國家優先：KR/TH 過 CF，JP 唔一定）
python3 - <<'PY' > /tmp/home_nodes.txt 2>/dev/null
import json, urllib.request
d = json.load(urllib.request.urlopen("http://127.0.0.1:9091/proxies", timeout=20))["proxies"]
home = [k for k in d if "家宽" in k]
PRIO = ("KR", "TH", "VN", "TW", "HK", "SG")
def rank(n):
    for i, c in enumerate(PRIO):
        if c + "-家宽" in n:
            return i
    return len(PRIO)
for k in sorted(home, key=rank):
    print(k)
PY
TOTAL=$(wc -l < /tmp/home_nodes.txt | tr -d ' ')
echo "[proxy] 家宽落地 ${TOTAL} 條，逐條試（最多 ${MIHOMO_TRIES} 條）"

N=0
while IFS= read -r NODE; do
  [ -z "$NODE" ] && continue
  N=$((N+1)); [ "$N" -gt "$MIHOMO_TRIES" ] && break
  python3 - "$NODE" <<'PY' 2>/dev/null || continue
import json, urllib.request, sys
req = urllib.request.Request("http://127.0.0.1:9091/proxies/GLOBAL", method="PUT",
      data=json.dumps({"name": sys.argv[1]}).encode(),
      headers={"content-type": "application/json"})
urllib.request.urlopen(req, timeout=15)
PY
  sleep 4
  CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 \
           --socks5-hostname 127.0.0.1:1080 https://optiklink.com/ 2>/dev/null)
  case "$CODE" in
    200|301|302)
      echo "[proxy] ✅ 揀到通到 optiklink 嘅住宅落地：${NODE}（HTTP ${CODE}）"
      if [ -n "${GITHUB_ENV:-}" ]; then
        echo "PROXY_SERVER=socks5h://127.0.0.1:1080" >> "$GITHUB_ENV"
        echo "PROXY_NODE=${NODE}" >> "$GITHUB_ENV"
      fi
      exit 0
      ;;
    *)
      echo "[proxy] ✗ ${NODE} → HTTP ${CODE:-000}（換下一條）"
      ;;
  esac
done < /tmp/home_nodes.txt

echo "[proxy] ✗ 試完 ${N} 條都冇一條通 optiklink（軌 1 照跑，軌 2 會直連）"
exit 0

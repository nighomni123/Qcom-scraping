#!/usr/bin/env bash
# qcapi_probe.sh — live-test QuickCommerce API's claimed anti-bot bypass.
#
# Part of option (b) from docs/quickcommerce-api-vetting.md. Reproduces the
# exact case where Moneymaker's own crawler is currently blocked: Instamart
# (their API name "Swiggy") at a Mumbai (Andheri West) lat/lon.
#
# Usage:
#   bash scripts/qcapi_probe.sh <API_KEY>
#   QCAPI_KEY=<KEY> QCAPI_PLATFORM=Swiggy QCAPI_LAT=19.1306 QCAPI_LON=72.8347 \
#     QCAPI_Q=milk bash scripts/qcapi_probe.sh
#
# Requires: curl, python3 (for compact JSON). The 100 free signup credits are
# enough for this probe (3 authenticated calls = 3 credits).

set -u

API="${QCAPI_BASE:-https://api.quickcommerceapi.com}"
KEY="${1:-${QCAPI_KEY:-}}"
PLATFORM="${QCAPI_PLATFORM:-Swiggy}"      # Swiggy == Instamart in their naming
LAT="${QCAPI_LAT:-19.1306}"               # Andheri West, Mumbai
LON="${QCAPI_LON:-72.8347}"
Q="${QCAPI_Q:-milk}"

if [ -z "$KEY" ]; then
  echo "ERROR: no API key. Pass as arg or set QCAPI_KEY / env." >&2
  echo "Sign up at https://quickcommerceapi.com/auth/signup (verify email for 100 free credits)." >&2
  exit 2
fi

pp() { python3 -c '
import sys,json,re
raw=sys.stdin.read()
# strip a trailing "[HTTP ...]" status line appended by curl -w
raw=re.sub(r"\n\[HTTP [^\n]*\]\s*$","",raw.strip())
try:
  d=json.loads(raw)
  print(json.dumps(d, indent=2, ensure_ascii=False)[:4000])
except Exception:
  print(raw[:4000] or "(empty body)")' ; }

echo "================================================================"
echo "QuickCommerce API live probe"
echo "target: platform=$PLATFORM  lat=$LAT lon=$LON  q=$Q"
echo "from IP/egress of this machine (where Moneymaker crawler is blocked)"
echo "================================================================"

echo; echo ">>> [1] PUBLIC /v1/supported-platforms (no key) — reachability"
curl -s -m 20 "$API/v1/supported-platforms" -w "\n[HTTP %{http_code} | %{time_total}s]\n" | pp

echo; echo ">>> [2] AUTHENTICATED /v1/search platform=$PLATFORM (the blocked case)"
curl -s -m 30 "$API/v1/search?q=$Q&lat=$LAT&lon=$LON&platform=$PLATFORM" \
  -H "X-API-Key: $KEY" -w "\n[HTTP %{http_code} | %{time_total}s]\n" | pp

echo; echo ">>> [3] AUTHENTICATED /v1/eta platform=$PLATFORM (store ids + open status)"
curl -s -m 30 "$API/v1/eta?lat=$LAT&lon=$LON&platform=$PLATFORM" \
  -H "X-API-Key: $KEY" -w "\n[HTTP %{http_code} | %{time_total}s]\n" | pp

echo; echo ">>> [4] /v1/credits (remaining balance check — free endpoint)"
curl -s -m 20 "$API/v1/credits" -H "X-API-Key: $KEY" \
  -w "\n[HTTP %{http_code}]\n" | pp

echo; echo "Done. If [2]/[3] return products/store_ids, their bypass works where ours is walled."

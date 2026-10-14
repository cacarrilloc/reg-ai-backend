#!/usr/bin/env bash
# Verify Railway backend ↔ Vercel frontend integration.
#
# Usage:
#   ./scripts/verify-railway-integration.sh \
#     https://reg-ai-backend-production.up.railway.app \
#     https://reg-ai-frontend.vercel.app

set -euo pipefail

BACKEND_URL="${1:-}"
FRONTEND_URL="${2:-https://reg-ai-frontend.vercel.app}"

if [[ -z "${BACKEND_URL}" ]]; then
  echo "Usage: $0 <railway-backend-url> [vercel-frontend-url]" >&2
  exit 1
fi

BACKEND_URL="${BACKEND_URL%/}"
FRONTEND_URL="${FRONTEND_URL%/}"

fail=0

check() {
  local name="$1"
  local ok="$2"
  if [[ "${ok}" == "true" ]]; then
    echo "PASS  ${name}"
  else
    echo "FAIL  ${name}"
    fail=$((fail + 1))
  fi
}

echo "Backend:  ${BACKEND_URL}"
echo "Frontend: ${FRONTEND_URL}"
echo

health_json=""
if health_json="$(curl -fsS "${BACKEND_URL}/api/v1/health")"; then
  check "GET /api/v1/health" true
  echo "      ${health_json}"
  while IFS= read -r line; do
    if [[ "${line}" == PASS* ]]; then
      echo "${line}"
    else
      echo "FAIL  health.${line#FAIL  }"
      fail=$((fail + 1))
    fi
  done < <(HEALTH_JSON="${health_json}" python3 - <<'PY'
import json, os, sys

data = json.loads(os.environ["HEALTH_JSON"])
checks = [
    ("environment is production", data.get("environment") == "production"),
    ("cors_frontend_configured", bool(data.get("cors_frontend_configured"))),
    ("database_configured", bool(data.get("database_configured"))),
    ("database_reachable", data.get("database_reachable") is True),
    ("clerk_configured", bool(data.get("clerk_configured"))),
    ("supabase_configured", bool(data.get("supabase_configured"))),
    ("auth_bypass disabled", data.get("auth_bypass_enabled") is False),
    ("deployment_ready", bool(data.get("deployment_ready"))),
]
for name, ok in checks:
    print(("PASS" if ok else "FAIL") + f"  {name}")
PY
)
else
  check "GET /api/v1/health" false
fi

cors_status="$(curl -sS -o /dev/null -w '%{http_code}' -X OPTIONS \
  "${BACKEND_URL}/api/v1/reviews/audit-chains" \
  -H "Origin: ${FRONTEND_URL}" \
  -H "Access-Control-Request-Method: GET" \
  -H "Access-Control-Request-Headers: authorization,content-type,x-org-id")"
check "CORS preflight for ${FRONTEND_URL} (HTTP ${cors_status})" "$([[ "${cors_status}" == "200" ]] && echo true || echo false)"

docs_status="$(curl -sS -o /dev/null -w '%{http_code}' "${BACKEND_URL}/docs")"
check "OpenAPI docs reachable (HTTP ${docs_status})" "$([[ "${docs_status}" == "200" ]] && echo true || echo false)"

echo
if [[ "${fail}" -gt 0 ]]; then
  echo "${fail} check(s) failed. See docs/railway-deployment.md for setup steps."
  exit 1
fi

echo "All checks passed. Set Vercel NEXT_PUBLIC_API_BASE_URL=${BACKEND_URL}"

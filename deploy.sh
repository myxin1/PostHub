#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_NAME="${PROJECT_NAME:-posthub}"

cd "$PROJECT_DIR"

print_section() {
  printf '\n'
  printf '%s\n' '========================================================================'
  printf '  %s\n' "$1"
  printf '%s\n' '========================================================================'
  printf '\n'
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'ERROR: command not found: %s\n' "$1" >&2
    exit 1
  fi
}

load_env_file() {
  local file="$1"
  if [[ -f "$file" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$file"
    set +a
  fi
}

prompt_value() {
  local label="$1"
  local default_value="${2:-}"
  local result=""
  if [[ -n "$default_value" ]]; then
    read -r -p "$label [$default_value]: " result
    printf '%s' "${result:-$default_value}"
  else
    read -r -p "$label: " result
    printf '%s' "$result"
  fi
}

prompt_secret() {
  local label="$1"
  local default_value="${2:-}"
  local result=""
  while true; do
    if [[ -n "$default_value" ]]; then
      read -r -s -p "$label [press enter to keep current]: " result
      printf '\n'
      result="${result:-$default_value}"
    else
      read -r -s -p "$label: " result
      printf '\n'
    fi
    if [[ ${#result} -ge 6 ]]; then
      printf '%s' "$result"
      return
    fi
    printf 'Password must have at least 6 characters.\n' >&2
  done
}

new_hex_secret() {
  python -c "import secrets; print(secrets.token_hex(32))"
}

new_base64_secret() {
  python -c "import base64, os; print(base64.b64encode(os.urandom(32)).decode())"
}

remove_env_if_present() {
  local key="$1"
  local target_env="$2"
  vercel env rm "$key" "$target_env" --yes >/dev/null 2>&1 || true
}

set_env_value() {
  local key="$1"
  local value="$2"
  if [[ -z "$value" ]]; then
    return
  fi
  local target_env
  for target_env in production preview; do
    remove_env_if_present "$key" "$target_env"
    printf '%s\n' "$value" | vercel env add "$key" "$target_env" >/dev/null
  done
  printf '  - %s\n' "$key"
}

print_section "PostHUB deploy for Vercel"

require_command vercel
require_command python

load_env_file "$PROJECT_DIR/backend/.env"
load_env_file "$PROJECT_DIR/.env.local"
load_env_file "$PROJECT_DIR/.env.vercel"

if ! vercel whoami >/dev/null 2>&1; then
  printf 'You are not logged into Vercel yet.\n' >&2
  printf 'Run: vercel login\n' >&2
  exit 1
fi

VERCEL_USER="$(vercel whoami 2>/dev/null)"
printf 'Vercel account: %s\n' "$VERCEL_USER"

print_section "Project link"
if [[ -f "$PROJECT_DIR/.vercel/project.json" ]]; then
  printf 'Project already linked through .vercel/project.json\n'
else
  vercel --yes --name "$PROJECT_NAME" 2>&1 | tail -5
fi

print_section "Runtime settings"
printf 'Leave DATABASE_URL empty to keep the current remote value.\n'
printf 'If the project has no DATABASE_URL yet, Vercel will fall back to temporary SQLite.\n\n'
DB_URL="$(prompt_value "DATABASE_URL" "${DATABASE_URL:-}")"
ADMIN_LOGIN="$(prompt_value "Admin login" "${POSTHUB_ADMIN_LOGIN:-adm}")"
ADMIN_EMAIL="$(prompt_value "Admin email" "${POSTHUB_ADMIN_EMAIL:-admin@posthub.local}")"
ADMIN_PASS="$(prompt_secret "Admin password" "${POSTHUB_ADMIN_PASSWORD:-}")"
BASE_URL_VALUE="$(prompt_value "BASE_URL for Google OAuth (optional)" "${BASE_URL:-}")"

JWT_SECRET_VALUE="${JWT_SECRET:-$(new_hex_secret)}"
JWT_ISSUER_VALUE="${JWT_ISSUER:-posthub}"
JWT_AUDIENCE_VALUE="${JWT_AUDIENCE:-posthub}"
ACCESS_TOKEN_TTL_VALUE="${ACCESS_TOKEN_TTL_SECONDS:-43200}"
SESSION_SECRET_VALUE="${SESSION_SECRET:-$(new_hex_secret)}"
ENCRYPTION_KEY_B64_VALUE="${ENCRYPTION_KEY_B64:-$(new_base64_secret)}"
CRON_SECRET_VALUE="${CRON_SECRET:-$(new_hex_secret)}"
WORDPRESS_TIMEOUT_VALUE="${WORDPRESS_TIMEOUT_SECONDS:-30}"
HTTP_TIMEOUT_VALUE="${HTTP_TIMEOUT_SECONDS:-30}"
HTTP_SKIP_VERIFY_VALUE="${HTTP_INSECURE_SKIP_VERIFY:-true}"

print_section "Uploading environment variables"

if [[ -n "$DB_URL" ]]; then
  set_env_value "DATABASE_URL" "$DB_URL"
fi

set_env_value "JWT_SECRET" "$JWT_SECRET_VALUE"
set_env_value "JWT_ISSUER" "$JWT_ISSUER_VALUE"
set_env_value "JWT_AUDIENCE" "$JWT_AUDIENCE_VALUE"
set_env_value "ACCESS_TOKEN_TTL_SECONDS" "$ACCESS_TOKEN_TTL_VALUE"
set_env_value "SESSION_SECRET" "$SESSION_SECRET_VALUE"
set_env_value "ENCRYPTION_KEY_B64" "$ENCRYPTION_KEY_B64_VALUE"
set_env_value "POSTHUB_ADMIN_LOGIN" "$ADMIN_LOGIN"
set_env_value "POSTHUB_ADMIN_EMAIL" "$ADMIN_EMAIL"
set_env_value "POSTHUB_ADMIN_PASSWORD" "$ADMIN_PASS"
set_env_value "CRON_SECRET" "$CRON_SECRET_VALUE"
set_env_value "WORDPRESS_TIMEOUT_SECONDS" "$WORDPRESS_TIMEOUT_VALUE"
set_env_value "HTTP_TIMEOUT_SECONDS" "$HTTP_TIMEOUT_VALUE"
set_env_value "HTTP_INSECURE_SKIP_VERIFY" "$HTTP_SKIP_VERIFY_VALUE"
set_env_value "POSTHUB_INLINE_WORKER" "0"

if [[ -n "$BASE_URL_VALUE" ]]; then
  set_env_value "BASE_URL" "$BASE_URL_VALUE"
fi

if [[ -n "${GEMINI_API_KEY:-}" ]]; then
  set_env_value "GEMINI_API_KEY" "$GEMINI_API_KEY"
  set_env_value "GEMINI_MODEL" "${GEMINI_MODEL:-gemini-1.5-flash-latest}"
fi

if [[ -n "${GOOGLE_CLIENT_ID:-}" ]]; then
  set_env_value "GOOGLE_CLIENT_ID" "$GOOGLE_CLIENT_ID"
  set_env_value "GOOGLE_CLIENT_SECRET" "${GOOGLE_CLIENT_SECRET:-}"
fi

print_section "Production deploy"
DEPLOY_OUTPUT="$(vercel --prod --yes 2>&1)"
printf '%s\n' "$DEPLOY_OUTPUT" | tail -10

PROD_URL="$(printf '%s\n' "$DEPLOY_OUTPUT" | grep -Eo 'https://[^ ]+\.vercel\.app' | tail -1 || true)"
if [[ -n "$PROD_URL" ]]; then
  print_section "Post-deploy setup"
  if command -v curl >/dev/null 2>&1; then
    if curl -fsSL "$PROD_URL/api/setup" >/dev/null; then
      printf 'Setup endpoint completed successfully.\n'
    else
      printf 'Warning: could not confirm %s/api/setup\n' "$PROD_URL" >&2
    fi
  else
    printf 'curl not found, skipping automatic /api/setup call.\n'
  fi
fi

print_section "Done"
printf 'Production URL: %s\n' "${PROD_URL:-not detected automatically}"
printf 'Admin login: %s\n' "$ADMIN_LOGIN"
printf 'Worker mode on Vercel: 0 (cron/serverless-safe)\n'
printf '\n'
printf 'If you use Google OAuth, make sure BASE_URL matches the public app URL.\n'

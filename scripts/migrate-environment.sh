#!/usr/bin/env bash
# Apply Alembic migrations to a staging or production Supabase database.
#
# Usage:
#   ./scripts/migrate-environment.sh staging
#   ./scripts/migrate-environment.sh production
#
# Requires DATABASE_URL in .env.staging / .env.prod (session pooler :5432,
# postgresql+psycopg://..., no ?pgbouncer=true).

set -euo pipefail

ENV_NAME="${1:-}"
if [[ "$ENV_NAME" != "staging" && "$ENV_NAME" != "production" ]]; then
  echo "Usage: $0 staging|production" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ENV_FILE=".env.staging"
if [[ "$ENV_NAME" == "production" ]]; then
  ENV_FILE=".env.prod"
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE — copy from deploy/railway.env.${ENV_NAME}.example and fill secrets." >&2
  exit 1
fi

# shellcheck disable=SC1090
set -a
# Load only DATABASE_URL / DIRECT_URL style lines (avoid executing arbitrary shell).
DATABASE_URL="$(grep -E '^DATABASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//')"
set +a

if [[ -z "${DATABASE_URL:-}" || "$DATABASE_URL" == *"REPLACE"* || "$DATABASE_URL" == *"PASSWORD"* ]]; then
  echo "DATABASE_URL in $ENV_FILE is missing or still a placeholder." >&2
  exit 1
fi

if [[ "$DATABASE_URL" == *"pgbouncer=true"* ]]; then
  echo "Remove ?pgbouncer=true from DATABASE_URL (Prisma-only; breaks SQLAlchemy)." >&2
  exit 1
fi

if [[ "$DATABASE_URL" != postgresql+psycopg://* && "$DATABASE_URL" != postgresql://* ]]; then
  echo "DATABASE_URL must start with postgresql+psycopg:// or postgresql://" >&2
  exit 1
fi

# Prefer explicit psycopg3 driver for Alembic.
if [[ "$DATABASE_URL" == postgresql://* ]]; then
  DATABASE_URL="postgresql+psycopg://${DATABASE_URL#postgresql://}"
fi

export DATABASE_URL
echo "Running alembic upgrade head against $ENV_NAME ..."
if [[ -x .venv/bin/alembic ]]; then
  .venv/bin/alembic upgrade head
else
  python -m alembic upgrade head
fi
echo "Done: $ENV_NAME schema is at head."

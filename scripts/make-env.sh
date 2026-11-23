#!/bin/sh
# Creates .env with strong random secrets for docker compose. Run once, then keep .env safe:
# store a copy of ENCRYPTION_KEY in your password manager or secret store, NOT with database backups.
set -eu
cd "$(dirname "$0")/.."
if [ -e .env ]; then
  echo ".env already exists; not overwriting it." >&2
  exit 1
fi
rand() { python3 -c "import secrets; print(secrets.token_urlsafe($1))"; }
fernet() { python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"; }
umask 077
cat > .env <<ENV
# Public hostname (leave as localhost for local testing)
DOMAIN=localhost
SECRET_KEY=$(rand 48)
ENCRYPTION_KEY=$(fernet)
# Give this to practices you invite so they can sign up
SIGNUP_CODE=$(rand 12)
ENV
echo "Created .env (readable only by you). Back up ENCRYPTION_KEY somewhere safe and separate."

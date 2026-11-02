#!/usr/bin/env bash
# Prints Render env lines to THIS terminal only. Run: bash scripts/render_env.sh  (reads .env.deploy and .env)
cd "$(dirname "$0")/.."
g(){ grep -h "^$1=" .env.deploy .env 2>/dev/null | head -1 | cut -d= -f2-; }
echo "DATABASE_URL=$(g DATABASE_URL)"
echo "S3_ENDPOINT_URL=$(g S3_ENDPOINT)"
echo "S3_ACCESS_KEY_ID=$(g B2_KEY_ID)"
echo "S3_SECRET_ACCESS_KEY=$(g B2_APP_KEY)"
echo "S3_BUCKET=$(g S3_BUCKET)"
echo "S3_REGION=$(g S3_REGION)"
echo "ADMIN_TOKEN=$(g ADMIN_TOKEN)"
echo "KEY_ENC_SECRET=$(g KEY_ENC_SECRET)"
echo "PUBLIC_BASE_URL=(isi setelah deploy: https://<nama>.onrender.com)"

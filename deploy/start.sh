#!/bin/bash
# Runs the API on loopback and the web app on $PORT; if either exits, the container exits.
set -euo pipefail
cd /app/backend
uvicorn app.main:app --host 127.0.0.1 --port 8000 &
cd /app/frontend
node node_modules/next/dist/bin/next start -H 0.0.0.0 -p "${PORT:-3000}" &
wait -n
exit $?

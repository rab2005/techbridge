#!/bin/bash
# Publish the current database to the live site.
#
#   scripts/publish.sh
#
# Copies data/techbridge.db into deploy/techbridge.db (a safe snapshot, even while
# the daily update is running), commits it, and pushes. Render redeploys on push.
# Each publish adds roughly 15MB to the repository's history, so publish when there's
# something worth updating (e.g. weekly, or after a summary run), not every day.

set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT"

mkdir -p deploy
rm -f deploy/techbridge.db
# .backup makes a consistent copy even if another process is writing.
sqlite3 data/techbridge.db ".backup deploy/techbridge.db"

COUNTS=$(sqlite3 deploy/techbridge.db \
  "SELECT COUNT(*) || ' technologies, ' || COUNT(enriched_at) || ' with AI summaries' FROM technologies")
echo "Snapshot: $COUNTS"

git add deploy/techbridge.db
if git diff --cached --quiet; then
  echo "Nothing changed since the last publish."
  exit 0
fi
git commit -m "Publish database snapshot ($COUNTS)"
git push
echo "Pushed. Render will redeploy in a few minutes."

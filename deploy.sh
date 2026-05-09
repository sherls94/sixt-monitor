#!/bin/bash
set -e

echo "=== Pushing to GitHub ==="
git add -A
git diff --cached --quiet && echo "Nothing to commit" || git commit -m "Deploy $(date '+%Y-%m-%d %H:%M')"
git push origin master

echo "=== Deploying to server ==="
ssh root@178.156.231.79 '
  cd /root/sixt-monitor &&
  git fetch origin &&
  git reset --hard origin/master &&
  echo "Pull complete" &&
  systemctl restart cardvault-scheduler &&
  echo "Scheduler restarted"
'

echo "=== Done ==="

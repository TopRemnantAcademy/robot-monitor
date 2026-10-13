#!/usr/bin/env bash
set -u
REPO=flow.237/RobotMonitor
SN=cnb-arc-1k4pnh9d8
CNB=/c/Users/piaoy/.workbuddy/binaries/node/cli-connector-packages/cnb
LOG=/c/Desktop/ROBOT/robot-monitor/issue_bodies/g7-watch.log
echo "watch start $(date -u +%FT%TZ)" > "$LOG"
for i in $(seq 1 30); do
  OUT=$("$CNB" build get-build-status --repo "$REPO" --sn "$SN" 2>/dev/null)
  ST=$(printf '%s' "$OUT" | grep -m1 'status:' | sed 's/.*status:[[:space:]]*//' | tr -d '\r')
  echo "iter $i $(date -u +%FT%TZ) status=$ST" >> "$LOG"
  if [ "$ST" = "success" ] || [ "$ST" = "error" ] || [ "$ST" = "cancel" ]; then
    echo "TERMINAL=$ST" >> "$LOG"
    printf '%s' "$OUT" | grep -i 'workMode\|npc go\|402\|EXCEEDING\|error' >> "$LOG" || true
    break
  fi
  PR=$("$CNB" pulls list-pulls --repo "$REPO" --state open 2>/dev/null | grep -i -m1 'g7\|agentverse\|#114' || true)
  if [ -n "$PR" ]; then echo "PR_FOUND=$PR" >> "$LOG"; break; fi
  sleep 120
done
echo "watch end $(date -u +%FT%TZ)" >> "$LOG"

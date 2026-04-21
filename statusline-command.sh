#!/usr/bin/env bash
# Claude Code status line: context usage and cross-session token costs by model

input=$(cat)

model=$(echo "$input" | jq -r '.model.display_name // "unknown"')
used_pct=$(echo "$input" | jq -r '.context_window.used_percentage // empty')

parts=()

# Context window usage
if [ -n "$used_pct" ]; then
  used_int=$(printf '%.0f' "$used_pct")
  parts+=("ctx:${used_int}%")
fi

# Cross-session token cost stats (cached for 2 min, includes subagents)
stats=$(python3 ~/.claude/token-stats.py 2>/dev/null)
if [ -n "$stats" ]; then
  # Daily total
  if echo "$stats" | jq -e '.daily.total > 0.005' >/dev/null 2>&1; then
    daily_fmt=$(echo "$stats" | jq -r '.daily.total_fmt')
    parts+=("today:${daily_fmt}")
  fi

  # Monthly total
  if echo "$stats" | jq -e '.monthly.total > 0.005' >/dev/null 2>&1; then
    monthly_fmt=$(echo "$stats" | jq -r '.monthly.total_fmt')
    parts+=("month:${monthly_fmt}")
  fi

  # Unknown-model warning
  if echo "$stats" | jq -e '.unknown_models | length > 0' >/dev/null 2>&1; then
    parts+=("?cost")
  fi
fi

parts+=("$model")

printf '%s' "${parts[*]}"

# claude-token-stats

Estimates Claude Code token costs from local session transcripts, broken down by model for today and the current month. Designed to feed a status-line display, but outputs plain JSON so it's easy to pipe into anything.

## How it works

Claude Code writes every conversation turn to JSONL files under `~/.claude/projects/<project-hash>/<session-id>.jsonl`. This script scans all those files, extracts assistant turns that carry usage data, and computes token costs for the current day and calendar month (in local time). Results are cached for 120 seconds so that rapid status-line refreshes don't re-scan everything each time.

Key details:
- **Deduplication:** each API call is streamed as multiple JSONL entries (partial chunks with `stop_reason=null`, then a final entry with `stop_reason` set). Only the final entry per `requestId` is counted.
- **Subagents:** subagent sessions live in subdirectories of the same projects tree and are picked up automatically by the recursive glob.
- **Recency filter:** files not modified in the last 32 days are skipped to keep scans fast on large project trees.
- **Pricing:** loaded from `~/.claude/model-prices.json`, auto-refreshed from [platform.claude.com](https://platform.claude.com/docs/en/about-claude/pricing) when more than one day old.

## Installation

Clone the repo, then link or copy `token-stats.py` into `~/.claude/` so the status-line script can find it at the standard path:

```sh
git clone https://github.com/emcsween/claude-token-stats ~/github/claude-token-stats
ln -s ~/github/claude-token-stats/token-stats.py ~/.claude/token-stats.py
```

Or just copy it if you prefer not to keep a live checkout:

```sh
cp token-stats.py ~/.claude/token-stats.py
```

The two data files it reads and writes both live under `~/.claude/`:

| File | Purpose |
|---|---|
| `~/.claude/model-prices.json` | Cached pricing table, auto-fetched |
| `~/.claude/token-stats-cache.json` | Cached scan results (2-minute TTL) |

## Usage

```sh
python3 token-stats.py            # use cached result if fresh
python3 token-stats.py --refresh  # force rescan and price refresh
```

## Output format

```json
{
  "generated_at": 1234567890.0,
  "unknown_models": ["model-id"],
  "daily":   { "total": 0.42, "total_fmt": "$0.42", "models": [...] },
  "monthly": { "total": 3.17, "total_fmt": "$3.17", "models": [...] }
}
```

Each entry in `models`:
```json
{ "name": "sonnet-4-6", "cost": 0.42, "cost_fmt": "$0.42", "input": 123456, "output": 7890 }
```

`unknown_models` lists any model IDs that appeared in the transcripts but had no matching pricing entry. A `?cost` warning is shown in the status line when this list is non-empty.

## Adding to the Claude Code status line

Run `/statusline` inside Claude Code and point it at a shell script that calls `token-stats.py` and formats the output. Claude can write this script for you — just describe what you want to see in the status line and ask it to set up `/statusline`.

`statusline-command.sh` in this repo is one example. It produces output like:

```
ctx:42% today:$0.12 month:$3.17 Claude Sonnet 4.6
```

and requires `jq` and `python3`.

#!/usr/bin/env python3
"""Estimate Claude Code token costs from local session transcripts. See README.md."""

import json
import re
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

CACHE_PATH = Path.home() / ".claude" / "token-stats-cache.json"
PRICES_PATH = Path.home() / ".claude" / "model-prices.json"
CACHE_TTL = 120        # seconds
PRICES_TTL = 86400     # seconds (1 day)
PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing"

force_refresh = "--refresh" in sys.argv


# ---------------------------------------------------------------------------
# Price fetching
# ---------------------------------------------------------------------------

class TableExtractor(HTMLParser):
    """Walks HTML and collects all tables as list[list[list[str]]]."""

    def __init__(self):
        super().__init__()
        self.tables: list = []
        self._table: list | None = None
        self._row: list | None = None
        self._cell: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = ""

    def handle_endtag(self, tag):
        if tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None
        elif tag == "tr" and self._row is not None:
            if self._table is not None and self._row:
                self._table.append(self._row)
            self._row = None
        elif tag in ("td", "th") and self._cell is not None:
            if self._row is not None:
                self._row.append(re.sub(r"\s+", " ", self._cell).strip())
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell += data


def model_name_to_pattern(name: str) -> str:
    """'Claude Opus 4.7 (deprecated)' -> 'opus-4-7'"""
    name = re.sub(r"\s*\([^)]*\)", "", name).strip()
    name = re.sub(r"^Claude\s+", "", name, flags=re.IGNORECASE)
    return re.sub(r"[\s.]+", "-", name).lower()


def parse_price(cell: str) -> float:
    m = re.search(r"\$([\d.]+)", cell)
    if not m:
        raise ValueError(f"no price in {cell!r}")
    return float(m.group(1))


def fetch_and_save_prices():
    print(f"Fetching prices from {PRICING_URL} ...", file=sys.stderr)
    req = urllib.request.Request(PRICING_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8")

    extractor = TableExtractor()
    extractor.feed(html)

    table = next(
        (t for t in extractor.tables if t and "base input" in " ".join(t[0]).lower()),
        None,
    )
    if table is None:
        raise ValueError(
            "pricing table not found — the page may be client-side rendered"
        )

    rows = []
    for cells in table[1:]:
        if len(cells) < 6:
            continue
        try:
            rows.append({
                "pattern":        model_name_to_pattern(cells[0]),
                "input":          parse_price(cells[1]),
                "cache_write_5m": parse_price(cells[2]),
                "cache_write_1h": parse_price(cells[3]),
                "cache_read":     parse_price(cells[4]),
                "output":         parse_price(cells[5]),
            })
        except ValueError as e:
            print(f"warning: skipping {cells[0]!r}: {e}", file=sys.stderr)

    if not rows:
        raise ValueError("no pricing rows parsed")

    # Longer pattern = more specific; must come first for substring matching
    rows.sort(key=lambda r: -len(r["pattern"]))

    with open(PRICES_PATH, "w") as f:
        json.dump({"generated_at": time.time(), "models": rows}, f, indent=2)

    print(f"Saved {len(rows)} models to {PRICES_PATH}", file=sys.stderr)


def load_model_pricing() -> list:
    needs_refresh = force_refresh
    if not needs_refresh:
        try:
            with open(PRICES_PATH) as f:
                data = json.load(f)
            if time.time() - data.get("generated_at", 0) >= PRICES_TTL:
                needs_refresh = True
        except (IOError, OSError, json.JSONDecodeError, KeyError):
            needs_refresh = True

    if needs_refresh:
        try:
            fetch_and_save_prices()
        except Exception as e:
            print(f"warning: could not refresh prices: {e}", file=sys.stderr)

    with open(PRICES_PATH) as f:
        return [
            (r["pattern"],
             float(r["input"]),
             float(r["cache_write_5m"]),
             float(r["cache_write_1h"]),
             float(r["cache_read"]),
             float(r["output"]))
            for r in json.load(f)["models"]
        ]


MODEL_PRICING = load_model_pricing()


# ---------------------------------------------------------------------------
# Cost calculation
# ---------------------------------------------------------------------------

def _match_model(model_id: str):
    """Return (pattern, prices) for the first match, or None if unknown."""
    m = (model_id or "").lower()
    for pattern, *prices in MODEL_PRICING:
        if pattern in m:
            return pattern, prices
    return None


def token_cost(usage: dict, prices: list) -> float:
    p_in, p_cw5m, p_cw1h, p_cr, p_out = prices

    # Use per-duration cache creation counts when available
    cache_creation = usage.get("cache_creation", {})
    c5m = cache_creation.get("ephemeral_5m_input_tokens")
    c1h = cache_creation.get("ephemeral_1h_input_tokens")
    if c5m is not None and c1h is not None:
        cache_cost = c5m / 1e6 * p_cw5m + c1h / 1e6 * p_cw1h
    else:
        # Fall back to total at 5m rate (most common for Claude Code)
        cache_cost = usage.get("cache_creation_input_tokens", 0) / 1e6 * p_cw5m

    return (
        usage.get("input_tokens", 0) / 1e6 * p_in
        + usage.get("output_tokens", 0) / 1e6 * p_out
        + cache_cost
        + usage.get("cache_read_input_tokens", 0) / 1e6 * p_cr
    )


def fmt_cost(cost: float) -> str:
    return f"${cost:.2f}"


# ---------------------------------------------------------------------------
# JSONL scanning
# ---------------------------------------------------------------------------

def read_file_entries(path: Path, month_str: str) -> list:
    """Return (model_id, date_str, usage) for completed assistant entries in the given month."""
    # Each API call can appear multiple times in the JSONL:
    # - During streaming: stop_reason=null, partial output (skip these)
    # - On completion: stop_reason set, full output (keep one per requestId)
    # Deduplicate by requestId, keeping the last final entry seen.
    final_entries: dict = {}

    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "assistant":
                    continue
                msg = entry.get("message", {})
                if msg.get("stop_reason") is None:
                    continue
                rid = entry.get("requestId") or entry.get("uuid", "")
                if not rid:
                    continue
                final_entries[rid] = entry
    except (IOError, OSError):
        return []

    records = []
    for entry in final_entries.values():
        ts = entry.get("timestamp", "")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
            entry_date = dt.strftime("%Y-%m-%d")
            entry_month = dt.strftime("%Y-%m")
        except (ValueError, TypeError):
            continue
        if entry_month != month_str:
            continue
        msg = entry.get("message", {})
        usage = msg.get("usage")
        if not usage:
            continue
        records.append((msg.get("model", "unknown"), entry_date, usage))
    return records


def new_bucket():
    return {"cost": 0.0, "input": 0, "output": 0}


def scan():
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return build_result({}, {}, set())

    now = datetime.now().astimezone()
    today_str = now.strftime("%Y-%m-%d")
    month_str = now.strftime("%Y-%m")
    cutoff_mtime = time.time() - (32 * 24 * 3600)  # skip files untouched in 32+ days

    daily: dict = defaultdict(new_bucket)
    monthly: dict = defaultdict(new_bucket)
    unknown_models: set = set()

    for jsonl in projects_dir.rglob("*.jsonl"):
        try:
            if jsonl.stat().st_mtime < cutoff_mtime:
                continue
        except OSError:
            continue

        for model_id, entry_date, usage in read_file_entries(jsonl, month_str):
            match = _match_model(model_id)
            if match is None:
                if usage.get("input_tokens", 0) > 0:
                    unknown_models.add(model_id)
                continue
            pattern, prices = match
            cost = token_cost(usage, prices)

            monthly[pattern]["cost"] += cost
            monthly[pattern]["input"] += usage.get("input_tokens", 0)
            monthly[pattern]["output"] += usage.get("output_tokens", 0)

            if entry_date == today_str:
                daily[pattern]["cost"] += cost
                daily[pattern]["input"] += usage.get("input_tokens", 0)
                daily[pattern]["output"] += usage.get("output_tokens", 0)

    return build_result(daily, monthly, unknown_models)


def build_result(daily, monthly, unknown_models):
    def total_cost(d):
        return sum(v["cost"] for v in d.values())

    def model_list(buckets):
        return [
            {
                "name": k,
                "cost": v["cost"],
                "cost_fmt": fmt_cost(v["cost"]),
                "input": v["input"],
                "output": v["output"],
            }
            for k, v in sorted(buckets.items(), key=lambda x: -x[1]["cost"])
        ]

    daily_total = total_cost(daily)
    monthly_total = total_cost(monthly)

    return {
        "generated_at": time.time(),
        "unknown_models": sorted(unknown_models),
        "daily": {
            "total": daily_total,
            "total_fmt": fmt_cost(daily_total),
            "models": model_list(daily),
        },
        "monthly": {
            "total": monthly_total,
            "total_fmt": fmt_cost(monthly_total),
            "models": model_list(monthly),
        },
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def load_cached():
    try:
        with open(CACHE_PATH) as f:
            data = json.load(f)
        if time.time() - data.get("generated_at", 0) < CACHE_TTL:
            return data
    except (IOError, OSError, json.JSONDecodeError, KeyError):
        pass
    return None


def main():
    data = None if force_refresh else load_cached()
    if data is None:
        data = scan()
        try:
            with open(CACHE_PATH, "w") as f:
                json.dump(data, f)
        except (IOError, OSError):
            pass
    print(json.dumps(data))


if __name__ == "__main__":
    main()
"""CMC Macro Gate — a tiny, dependency-free macro risk-budget layer for any
trading engine, powered by the CoinMarketCap Skill Hub.

Architecture (the important idea):

    [ collector ]  --MCP/HTTP-->  CoinMarketCap Skill Hub
         |  writes
         v
    cmc_macro.json   (a small cached file on disk)
         |  reads (offline, no network)
         v
    [ MacroGate ]  -->  your strategy multiplies position size / vetoes

The trading hot path NEVER calls the network. A background collector refreshes
the cache file on an interval; the gate just reads that file. If the cache goes
stale (default > 24h) the gate degrades to a neutral 1.0 multiplier, so a missed
refresh can never silently strangle your sizing.

The daily_market_overview skill is research-only, so this is used as a
portfolio-wide RISK BUDGET (how much to size, whether to skip fresh longs),
NOT as a directional buy/sell signal.

Setup:
    export CMC_MCP_API_KEY=...        # get from your CoinMarketCap MCP access

Refresh the cache (run once, or as a daemon):
    python cmc_macro.py refresh
    python cmc_macro.py refresh --loop 3600     # hourly background daemon

Read it from your strategy:
    from cmc_macro import MacroGate
    gate = MacroGate("cmc_macro.json")
    f = gate.evaluate(direction="LONG")
    size = base_size * f["size_mult"]            # e.g. base * 0.5 on risk-off days
    if f["action"] == "VETO":
        skip_trade()
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from typing import Dict, Optional

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULT_URL = os.environ.get("CMC_MCP_URL", "https://mcp.coinmarketcap.com/skill-hub/stream")
CACHE_PATH = os.environ.get("CMC_MACRO_CACHE", "cmc_macro.json")
PROTOCOL_VERSION = "2025-03-26"

# regime -> base portfolio size multiplier (0-1)
REGIME_MULTIPLIER = {
    "tailwind_easing": 1.00,
    "supportive": 1.00,
    "neutral": 0.85,
    "mixed": 0.75,
    "headwind_tightening": 0.50,
    "risk_off": 0.40,
}


# ---------------------------------------------------------------------------
# Collector: fetch daily_market_overview over MCP-HTTP and write the cache
# ---------------------------------------------------------------------------
def _parse_sse(raw: str) -> dict:
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return json.loads(raw)


def _rpc(url: str, key: str, payload: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-CMC-MCP-API-KEY": key,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return _parse_sse(r.read().decode())


def fetch_overview(timeout: int = 180) -> dict:
    """Call the daily_market_overview skill and return the raw result dict."""
    key = os.environ.get("CMC_MCP_API_KEY")
    if not key:
        raise RuntimeError("Set CMC_MCP_API_KEY in your environment.")

    _rpc(DEFAULT_URL, key, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "cmc-macro-gate", "version": "1.0"},
        },
    }, timeout=30)

    resp = _rpc(DEFAULT_URL, key, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {
            "name": "execute_skill",
            "arguments": {"unique_name": "daily_market_overview",
                          "parameters": {"preview": True}},
        },
    }, timeout=timeout)

    if "error" in resp:
        raise RuntimeError(f"MCP error: {resp['error']}")
    for block in resp.get("result", {}).get("content", []):
        if block.get("type") == "text":
            return json.loads(block["text"])
    raise RuntimeError(f"Unexpected response: {json.dumps(resp)[:300]}")


def _dig(d, *path, default=None):
    cur = d
    for k in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def normalize(skill_result: dict) -> dict:
    """Map a daily_market_overview result into the compact cache schema."""
    data = skill_result
    for key in ("result", "data"):
        if isinstance(data, dict) and isinstance(data.get(key), dict):
            data = data[key]
    mr = _dig(data, "market_read", default={})
    fin = _dig(data, "macro_deep_read", "financial_conditions", default={})

    fear_greed = None
    for m in _dig(fin, "key_metrics", default=[]):
        if isinstance(m, str) and "Fear & Greed" in m:
            try:
                fear_greed = int(float(m.split()[-1]))
            except (ValueError, IndexError):
                pass

    return {
        "source": "cmc-skill-hub:daily_market_overview",
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "regime": _dig(mr, "regime", default="neutral"),
        "risk_bias": _dig(mr, "risk_bias", default=""),
        "composite_score": _dig(mr, "composite_score"),
        "risk_budget": _dig(mr, "risk_budget", default={}),
        "fear_greed": fear_greed,
        "_note": "research_only; macro size/veto gate, not a directional signal",
    }


def refresh(path: str = CACHE_PATH) -> dict:
    cache = normalize(fetch_overview())
    with open(path, "w") as f:
        json.dump(cache, f, indent=2)
    print(f"[cmc-macro] wrote {path}: regime={cache['regime']} "
          f"fear_greed={cache['fear_greed']} "
          f"budget={_dig(cache, 'risk_budget', 'max_position_pct')}%", flush=True)
    return cache


# ---------------------------------------------------------------------------
# Gate: read the cache and turn it into a size multiplier / action
# ---------------------------------------------------------------------------
class MacroGate:
    def __init__(self, cache_path: str = CACHE_PATH, max_age_hours: float = 24.0):
        self.cache_path = cache_path
        self.max_age_hours = max_age_hours

    def _load(self):
        try:
            data = json.load(open(self.cache_path))
        except (FileNotFoundError, json.JSONDecodeError):
            return None, None, True
        age = None
        stale = True
        ts = data.get("fetched_at")
        if ts:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
                stale = age > self.max_age_hours
            except ValueError:
                pass
        return data, age, stale

    def evaluate(self, direction: str = "") -> Dict:
        """Return {regime, fear_greed, size_mult (0-1), action, stale, note}."""
        data, age, stale = self._load()
        if not data or stale:
            return {"regime": (data or {}).get("regime", "UNKNOWN"),
                    "fear_greed": None, "size_mult": 1.0, "action": "NORMAL",
                    "stale": True, "note": f"stale/missing cache (age={age}h) -> neutral"}

        regime = data.get("regime", "neutral")
        fg = data.get("fear_greed")
        bias = data.get("risk_bias", "")
        budget = data.get("risk_budget", {})

        mult = REGIME_MULTIPLIER.get(regime, 0.85)
        mult = min(mult, budget.get("max_position_pct", 100) / 100.0)
        notes = [f"regime={regime}", f"budget={budget.get('max_position_pct')}%"]

        if isinstance(fg, (int, float)) and fg <= 20:
            mult *= 0.8
            notes.append(f"extreme_fear({fg})")

        action = "REDUCE" if mult <= 0.45 else "NORMAL"
        if "defensive" in bias and regime in ("headwind_tightening", "risk_off") \
                and direction.upper() == "LONG":
            action = "REDUCE"
            notes.append("defensive_long_caution")

        return {"regime": regime, "fear_greed": fg,
                "size_mult": round(max(0.0, min(1.0, mult)), 3),
                "action": action, "stale": False, "note": " | ".join(notes)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="CMC macro gate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("refresh", help="fetch overview and write the cache")
    r.add_argument("--loop", type=int, default=0, help="refresh every N seconds")
    r.add_argument("--out", default=CACHE_PATH)
    g = sub.add_parser("show", help="read the cache and print the gate decision")
    g.add_argument("--direction", default="LONG")
    g.add_argument("--cache", default=CACHE_PATH)
    args = ap.parse_args()

    if args.cmd == "refresh":
        while True:
            try:
                refresh(args.out)
            except Exception as e:
                print(f"[cmc-macro] refresh failed: {e}", file=sys.stderr, flush=True)
            if args.loop <= 0:
                break
            time.sleep(args.loop)
    elif args.cmd == "show":
        print(json.dumps(MacroGate(args.cache).evaluate(args.direction), indent=2))


if __name__ == "__main__":
    main()

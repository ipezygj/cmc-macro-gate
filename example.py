"""Minimal usage example: gate a trade with the cached CMC macro read.

Run `python cmc_macro.py refresh` first (needs CMC_MCP_API_KEY) so there is a
cmc_macro.json to read. This example itself does no network I/O.
"""
from cmc_macro import MacroGate

gate = MacroGate("cmc_macro.json")

# Pretend this is a signal coming out of your strategy
signal = {"symbol": "BTC", "direction": "LONG", "base_size_usd": 1000.0}

macro = gate.evaluate(direction=signal["direction"])

print(f"Macro regime : {macro['regime']} (Fear&Greed {macro['fear_greed']})")
print(f"Size mult    : x{macro['size_mult']}")
print(f"Action       : {macro['action']}  {'[STALE -> neutral]' if macro['stale'] else ''}")
print(f"Why          : {macro['note']}")

if macro["action"] == "VETO":
    print("\n-> SKIP trade (macro veto)")
else:
    sized = signal["base_size_usd"] * macro["size_mult"]
    print(f"\n-> Place {signal['symbol']} {signal['direction']} sized ${sized:,.0f} "
          f"(from ${signal['base_size_usd']:,.0f})")

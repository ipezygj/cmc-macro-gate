# CMC Macro Gate

A tiny, dependency-free **macro risk-budget layer** for any trading engine,
powered by the [CoinMarketCap Skill Hub](https://coinmarketcap.com). Pure Python
standard library — no `pip install` needed.

It pulls CMC's daily market overview (regime, Fear & Greed, suggested risk
budget) and turns it into a single **position-size multiplier** and an
**action** (`NORMAL` / `REDUCE` / `VETO`) your strategy can apply.

## The idea

```
[ collector ]  --MCP/HTTP-->  CoinMarketCap Skill Hub
     | writes
     v
cmc_macro.json   (small cached file on disk)
     | reads (offline, no network)
     v
[ MacroGate ]  -->  your strategy multiplies size / vetoes new longs
```

The **trading hot path never touches the network.** A background collector
refreshes the cache on an interval; the gate just reads the file. If the cache
goes stale (> 24h by default) the gate falls back to a neutral `1.0` multiplier,
so a missed refresh can never silently strangle your sizing.

> The underlying `daily_market_overview` skill is **research-only**, so this is a
> portfolio-wide *risk budget* (how big to size, whether to skip fresh longs),
> **not** a directional buy/sell signal.

## Setup

```bash
export CMC_MCP_API_KEY=...        # your CoinMarketCap MCP key
```

## Refresh the cache

```bash
python cmc_macro.py refresh                 # run once
python cmc_macro.py refresh --loop 3600     # background daemon, hourly
python cmc_macro.py show --direction LONG   # inspect current gate decision
```

Run the daemon alongside your other collectors (systemd, supervisor, nohup, …).

## Use it in a strategy

```python
from cmc_macro import MacroGate

gate = MacroGate("cmc_macro.json")
macro = gate.evaluate(direction="LONG")

size = base_size * macro["size_mult"]        # e.g. base * 0.5 on a risk-off day
if macro["action"] == "VETO":
    skip_trade()
```

`evaluate()` returns:

| field        | meaning                                              |
|--------------|------------------------------------------------------|
| `regime`     | e.g. `headwind_tightening`, `supportive`, `neutral`  |
| `fear_greed` | CMC Fear & Greed index (0–100), or `None`            |
| `size_mult`  | 0–1 multiplier to apply to your position size        |
| `action`     | `NORMAL` / `REDUCE` / `VETO`                          |
| `stale`      | `true` if cache too old → neutralized to `1.0`       |
| `note`       | short human-readable reason string                   |

## Tuning

All the mapping lives in two places in `cmc_macro.py`:

- `REGIME_MULTIPLIER` — regime → base size multiplier
- `MacroGate.evaluate()` — extreme-fear penalty, defensive-long caution, the
  `REDUCE`/`VETO` thresholds

Adjust to taste for your venue and risk appetite.

## Files

- `cmc_macro.py` — collector (`refresh`) + gate (`MacroGate`) + CLI, one file
- `example.py` — minimal end-to-end usage demo
- `cmc_macro.json` — generated cache (gitignore it)

## License

MIT — do whatever you like.

"""
Solana memecoin signal bot (PAPER mode) -> Discord embeds.

Pipeline: discover new tokens -> filter -> post an info embed to a Discord channel -> track the
signal as a paper trade -> post an exit embed at take-profit / stop-loss.
No wallet, no keys, no trades. You act on signals manually (e.g. in Axiom or Fomo).

Setup:   pip install requests
Env:     DISCORD_WEBHOOK_URL   (required) channel webhook the embeds are posted to
         SOLANA_RPC_URL        (optional) a Helius/QuickNode endpoint; the public one is rate-limited
Run:     python memecoin_bot.py          (loops forever; for a PC/server)
         python memecoin_bot.py --once   (single pass using state.json; for GitHub Actions/cron)
Test:    python memecoin_bot.py --test     (sends a fake sample embed so you can check the webhook)
"""
import csv
import json
import os
import sys
import time
from dataclasses import dataclass, field

import requests

RPC_URL = os.getenv("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"  # "or": an empty env var falls back too
DEXSCREENER = "https://api.dexscreener.com"
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")  # keep secret: anyone with it can post to your channel

GREEN, RED = 0x2ECC71, 0xE74C3C


@dataclass
class Config:
    min_liquidity_usd: float = 20_000
    min_volume_h1_usd: float = 10_000
    min_pair_age_min: float = 10        # skip brand-new launches (most dangerous)
    max_pair_age_min: float = 360
    position_size_usd: float = 25       # paper size only
    take_profit: float = 0.50           # +50%
    stop_loss: float = 0.20             # -20%
    max_open_positions: int = 3
    max_daily_loss_usd: float = 50
    poll_seconds: int = 20
    log_file: str = "paper_trades.csv"
    state_file: str = "state.json"      # used by --once mode (scheduled runs)


@dataclass
class Position:
    mint: str
    symbol: str
    entry_price: float
    size_usd: float
    url: str = ""
    opened_at: float = field(default_factory=time.time)


# ---------- Formatting helpers ----------
def usd(n):
    if n is None:
        return "n/a"
    n = float(n)
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            return f"${n / div:.2f}{suffix}"
    return f"${n:,.2f}"


def pct(n):
    return "n/a" if n is None else f"{float(n):+.1f}%"


def price_str(p):
    return "$" + f"{float(p):.10f}".rstrip("0").rstrip(".")


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------- Discord ----------
def notify(embed: dict):
    """Post one embed via webhook. A Discord failure must never break the bot."""
    if not DISCORD_WEBHOOK:
        print("[warn] DISCORD_WEBHOOK_URL not set; skipping alert")
        return
    try:
        for _ in range(2):  # one retry if rate limited
            r = requests.post(DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=10)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 1)))
                continue
            if r.status_code >= 400:
                print(f"[warn] discord returned {r.status_code}: {r.text[:200]}")
            break
    except Exception as e:
        print(f"[warn] discord notify failed: {e}")


def build_signal_embed(pair: dict, cfg: Config) -> dict:
    base = pair.get("baseToken") or {}
    mint = base.get("address", "?")
    price = float(pair["priceUsd"])
    liq = (pair.get("liquidity") or {}).get("usd")
    vol = pair.get("volume") or {}
    chg = pair.get("priceChange") or {}
    h1 = (pair.get("txns") or {}).get("h1") or {}
    info = pair.get("info") or {}
    age_h = (time.time() * 1000 - (pair.get("pairCreatedAt") or 0)) / 3_600_000
    mcap = pair.get("marketCap") or pair.get("fdv")

    links = []
    if pair.get("url"):
        links.append(f"[Chart]({pair['url']})")
    for w in info.get("websites") or []:
        if w.get("url"):
            links.append(f"[{w.get('label') or 'Website'}]({w['url']})")
    for s in info.get("socials") or []:
        if s.get("url"):
            links.append(f"[{(s.get('type') or 'link').title()}]({s['url']})")

    fields = [
        {"name": "Price", "value": price_str(price), "inline": True},
        {"name": "Market cap", "value": usd(mcap), "inline": True},
        {"name": "Liquidity", "value": usd(liq), "inline": True},
        {"name": "Volume", "value": f"5m {usd(vol.get('m5'))}\n1h {usd(vol.get('h1'))}\n24h {usd(vol.get('h24'))}", "inline": True},
        {"name": "Price change", "value": f"5m {pct(chg.get('m5'))}\n1h {pct(chg.get('h1'))}\n24h {pct(chg.get('h24'))}", "inline": True},
        {"name": "1h txns", "value": f"{h1.get('buys', 0)} buys / {h1.get('sells', 0)} sells", "inline": True},
        {"name": "Age / DEX", "value": f"{age_h:.1f}h on {pair.get('dexId', '?')}", "inline": True},
        {"name": "Checks passed", "value": f"Liquidity ≥ {usd(cfg.min_liquidity_usd)}, 1h volume ≥ {usd(cfg.min_volume_h1_usd)}, mint + freeze authority revoked", "inline": False},
        {"name": "NOT checked", "value": "Holder concentration, LP burned/locked, honeypot (sell) test. Verify these yourself before buying.", "inline": False},
        {"name": "Paper plan", "value": f"TP +{cfg.take_profit:.0%} ({price_str(price * (1 + cfg.take_profit))}) / SL -{cfg.stop_loss:.0%} ({price_str(price * (1 - cfg.stop_loss))})", "inline": False},
        {"name": "Mint (tap to copy, paste into Axiom/Fomo)", "value": f"```{mint}```", "inline": False},
    ]
    if links:
        fields.append({"name": "Links", "value": " • ".join(links)[:1000], "inline": False})

    embed = {
        "title": f"{base.get('symbol', '?')} — {base.get('name', '')}"[:256],
        "color": GREEN,
        "fields": fields,
        "footer": {"text": "Paper signal. Not financial advice, do your own research."},
        "timestamp": now_iso(),
    }
    if pair.get("url"):
        embed["url"] = pair["url"]
    if info.get("imageUrl"):
        embed["thumbnail"] = {"url": info["imageUrl"]}
    return embed


def build_exit_embed(pos: Position, price: float, pnl: float, reason: str) -> dict:
    change = price / pos.entry_price - 1
    return {
        "title": f"EXIT: {pos.symbol} ({reason.replace('_', ' ')})",
        "color": GREEN if pnl >= 0 else RED,
        "fields": [
            {"name": "Entry", "value": price_str(pos.entry_price), "inline": True},
            {"name": "Now", "value": price_str(price), "inline": True},
            {"name": "Change", "value": f"{change:+.1%}", "inline": True},
            {"name": "Paper PnL", "value": f"${pnl:.2f} on ${pos.size_usd:.0f} (ignores fees/slippage)", "inline": False},
            {"name": "Mint", "value": f"```{pos.mint}```", "inline": False},
        ],
        "footer": {"text": "Paper result. Real trades will do worse after fees and slippage."},
        "timestamp": now_iso(),
    }


# ---------- Data ----------
def get_json(url, **kwargs):
    try:
        r = requests.get(url, timeout=10, **kwargs)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[warn] request failed: {url} ({e})")
        return None


def latest_solana_mints():
    data = get_json(f"{DEXSCREENER}/token-profiles/latest/v1") or []
    return [d["tokenAddress"] for d in data if d.get("chainId") == "solana"]


def best_pair(mint):
    data = get_json(f"{DEXSCREENER}/latest/dex/tokens/{mint}")
    pairs = [p for p in (data or {}).get("pairs") or [] if p.get("chainId") == "solana"]
    if not pairs:
        return None
    return max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)


def mint_authorities_revoked(mint):
    """True if mint and freeze authority are both revoked (null). Fails closed on any error."""
    try:
        r = requests.post(RPC_URL, timeout=10, json={
            "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
            "params": [mint, {"encoding": "jsonParsed"}],
        })
        info = r.json()["result"]["value"]["data"]["parsed"]["info"]
        return info.get("mintAuthority") is None and info.get("freezeAuthority") is None
    except Exception as e:
        print(f"[warn] authority check failed for {mint}: {e}")
        return False


def passes_filters(pair, cfg: Config):
    liq = (pair.get("liquidity") or {}).get("usd") or 0
    vol_h1 = (pair.get("volume") or {}).get("h1") or 0
    created_ms = pair.get("pairCreatedAt")
    if not created_ms:
        return False
    age_min = (time.time() * 1000 - created_ms) / 60_000
    return (
        liq >= cfg.min_liquidity_usd
        and vol_h1 >= cfg.min_volume_h1_usd
        and cfg.min_pair_age_min <= age_min <= cfg.max_pair_age_min
        and mint_authorities_revoked(pair["baseToken"]["address"])
    )
    # TODO: holder concentration, LP burned/locked, honeypot/sell simulation


# ---------- Bot ----------
class PaperBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.positions: dict[str, Position] = {}
        self.seen: dict[str, None] = {}  # ordered, so old entries can be trimmed
        self.realized_pnl_today = 0.0
        self.day = time.strftime("%F")
        if not os.path.exists(cfg.log_file):
            with open(cfg.log_file, "w", newline="") as f:
                csv.writer(f).writerow(["time", "action", "symbol", "mint", "price", "pnl_usd", "reason"])

    def log(self, action, pos, price, pnl=0.0, reason=""):
        with open(self.cfg.log_file, "a", newline="") as f:
            csv.writer(f).writerow([time.strftime("%F %T"), action, pos.symbol, pos.mint, price, round(pnl, 2), reason])
        print(f"[{action}] {pos.symbol} @ {price} pnl={pnl:.2f} {reason}")

    def roll_day(self):
        today = time.strftime("%F")
        if today != self.day:
            self.day, self.realized_pnl_today = today, 0.0

    def scan(self):
        if self.realized_pnl_today <= -self.cfg.max_daily_loss_usd:
            return  # daily loss limit hit: no new signals today
        for mint in latest_solana_mints():
            if mint in self.seen or len(self.positions) >= self.cfg.max_open_positions:
                continue
            self.seen[mint] = None
            pair = best_pair(mint)
            if pair and passes_filters(pair, self.cfg):
                price = float(pair["priceUsd"])
                pos = Position(mint, pair["baseToken"]["symbol"], price, self.cfg.position_size_usd, url=pair.get("url", ""))
                self.positions[mint] = pos
                self.log("SIGNAL", pos, price)
                notify(build_signal_embed(pair, self.cfg))

    def manage(self):
        for mint, pos in list(self.positions.items()):
            pair = best_pair(mint)
            if not pair:
                continue
            price = float(pair["priceUsd"])
            change = price / pos.entry_price - 1
            reason = None
            if change >= self.cfg.take_profit:
                reason = "take_profit"
            elif change <= -self.cfg.stop_loss:
                reason = "stop_loss"
            if reason:
                pnl = pos.size_usd * change
                self.realized_pnl_today += pnl
                del self.positions[mint]
                self.log("EXIT", pos, price, pnl, reason)
                notify(build_exit_embed(pos, price, pnl, reason))

    def save_state(self):
        state = {
            "day": self.day,
            "realized_pnl_today": self.realized_pnl_today,
            "seen": list(self.seen)[-3000:],
            "positions": [vars(p) for p in self.positions.values()],
        }
        with open(self.cfg.state_file, "w") as f:
            json.dump(state, f)

    def load_state(self):
        if not os.path.exists(self.cfg.state_file):
            return
        try:
            with open(self.cfg.state_file) as f:
                s = json.load(f)
            self.day = s.get("day", self.day)
            self.realized_pnl_today = s.get("realized_pnl_today", 0.0)
            self.seen = dict.fromkeys(s.get("seen", []))
            self.positions = {p["mint"]: Position(**p) for p in s.get("positions", [])}
        except Exception as e:
            print(f"[warn] could not load state, starting fresh: {e}")

    def run_once(self):
        """One pass for schedulers (GitHub Actions, cron). State persists in state.json."""
        self.load_state()
        self.roll_day()
        self.manage()
        self.scan()
        self.save_state()

    def run(self):
        print("Signal bot started (paper mode). Ctrl+C to stop.")
        while True:
            self.roll_day()
            self.manage()
            self.scan()
            time.sleep(self.cfg.poll_seconds)


def send_test():
    """Fake token, just to check the webhook and how embeds look on your phone."""
    cfg = Config()
    pair = {
        "baseToken": {"address": "TestMint1111111111111111111111111111111111", "symbol": "TEST", "name": "Test Coin"},
        "priceUsd": "0.00004213", "liquidity": {"usd": 48_500}, "marketCap": 4_213_000,
        "volume": {"m5": 3_200, "h1": 61_000, "h24": 410_000},
        "priceChange": {"m5": 2.1, "h1": 18.4, "h24": 96.0},
        "txns": {"h1": {"buys": 412, "sells": 301}},
        "pairCreatedAt": (time.time() - 3 * 3600) * 1000, "dexId": "raydium",
        "url": "https://dexscreener.com/solana",
    }
    notify(build_signal_embed(pair, cfg))
    print("Test embed sent (check your Discord channel).")


if __name__ == "__main__":
    if "--test" in sys.argv:
        send_test()
    elif "--once" in sys.argv:
        PaperBot(Config()).run_once()
    else:
        PaperBot(Config()).run()

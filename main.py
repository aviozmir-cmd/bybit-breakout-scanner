
import os
import time
import statistics
import requests
from datetime import datetime, timezone

BASE = "https://api.bybit.com"
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Initial conservative signal settings
MAX_COINS = 100
MIN_TURNOVER_24H = 200_000
MAX_TURNOVER_24H = 20_000_000
MIN_CANDLE_TURNOVER = 20_000
VOLUME_MULTIPLE = 2.5
MIN_BREAKOUT_PCT = 1.2
MAX_ABOVE_RESISTANCE_PCT = 6.0
COOLDOWN_SECONDS = 90 * 60

session = requests.Session()
last_alert = {}
history = {}

def api(path, params=None):
    r = session.get(BASE + path, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(data.get("retMsg", "Bybit API error"))
    return data["result"]

def telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram credentials not configured.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = session.post(url, data={
        "chat_id": CHAT_ID,
        "text": message
    }, timeout=20)
    r.raise_for_status()

def get_symbols():
    instruments = api(
        "/v5/market/instruments-info",
        {"category": "spot", "limit": 1000}
    )["list"]
    tickers = api(
        "/v5/market/tickers",
        {"category": "spot"}
    )["list"]

    status = {
        x["symbol"]: x for x in instruments
        if x.get("status") == "Trading"
    }
    excluded = (
        "UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT"
    )
    stable_bases = {
        "USDC", "USDE", "DAI", "FDUSD", "TUSD",
        "USDP", "USDD", "EUR", "EURC"
    }

    candidates = []
    for t in tickers:
        s = t["symbol"]
        if not s.endswith("USDT") or s not in status:
            continue
        if s.endswith(excluded):
            continue
        if s[:-4] in stable_bases:
            continue
        try:
            turnover = float(t.get("turnover24h", 0))
            bid = float(t.get("bid1Price", 0))
            ask = float(t.get("ask1Price", 0))
        except (TypeError, ValueError):
            continue
        if not MIN_TURNOVER_24H <= turnover <= MAX_TURNOVER_24H:
            continue
        if bid <= 0 or ask <= 0:
            continue
        spread_pct = (ask - bid) / ((ask + bid) / 2) * 100
        if spread_pct > 0.5:
            continue
        candidates.append((turnover, s))

    candidates.sort(reverse=True)
    return [s for _, s in candidates[:MAX_COINS]]

def get_history(symbol):
    rows = api(
        "/v5/market/kline",
        {
            "category": "spot",
            "symbol": symbol,
            "interval": "5",
            "limit": 100
        }
    )["list"]

    # API returns newest first. Keep closed candles only.
    rows = sorted(rows, key=lambda x: int(x[0]))
    now_ms = int(time.time() * 1000)
    candles = []
    for row in rows:
        start = int(row[0])
        if start + 5 * 60 * 1000 <= now_ms:
            candles.append({
                "start": start,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "turnover": float(row[6])
            })
    return candles

def check_signal(symbol, candles):
    if len(candles) < 22:
        return None

    current = candles[-1]
    previous = candles[-21:-1]
    resistance = max(c["high"] for c in previous)
    avg_volume = statistics.mean(c["volume"] for c in previous)
    prior_close = candles[-2]["close"]

    if resistance <= 0 or avg_volume <= 0 or prior_close <= 0:
        return None

    breakout_pct = (current["close"] / resistance - 1) * 100
    change_pct = (current["close"] / prior_close - 1) * 100
    volume_multiple = current["volume"] / avg_volume

    if current["close"] <= resistance:
        return None
    if breakout_pct < MIN_BREAKOUT_PCT:
        return None
    if breakout_pct > MAX_ABOVE_RESISTANCE_PCT:
        return None
    if change_pct < MIN_BREAKOUT_PCT:
        return None
    if volume_multiple < VOLUME_MULTIPLE:
        return None
    if current["turnover"] < MIN_CANDLE_TURNOVER:
        return None

    return {
        "price": current["close"],
        "resistance": resistance,
        "breakout_pct": breakout_pct,
        "change_pct": change_pct,
        "volume_multiple": volume_multiple,
        "turnover": current["turnover"],
        "candle_time": current["start"]
    }

def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first."
        )

    telegram("✅ Bybit scanner started. Public data only; no trades.")
    while True:
        try:
            symbols = get_symbols()
            print(f"Scanning {len(symbols)} spot pairs")
            for symbol in symbols:
                try:
                    candles = get_history(symbol)
                    history[symbol] = candles
                    signal = check_signal(symbol, candles)
                    now = time.time()

                    if signal and now - last_alert.get(symbol, 0) >= COOLDOWN_SECONDS:
                        stamp = datetime.fromtimestamp(
                            signal["candle_time"] / 1000,
                            tz=timezone.utc
                        ).strftime("%Y-%m-%d %H:%M UTC")
                        message = (
                            f"🚨 5m BREAKOUT: {symbol}\n"
                            f"Price: {signal['price']:.8g} USDT\n"
                            f"Resistance: {signal['resistance']:.8g}\n"
                            f"Above resistance: {signal['breakout_pct']:.2f}%\n"
                            f"5m change: {signal['change_pct']:.2f}%\n"
                            f"Volume vs prior 20 candles: "
                            f"{signal['volume_multiple']:.2f}x\n"
                            f"Candle turnover: "
                            f"${signal['turnover']:,.0f}\n"
                            f"Candle closed: {stamp}\n"
                            "Watch for a close back below resistance; "
                            "signal is not a recommendation."
                        )
                        telegram(message)
                        last_alert[symbol] = now
                    time.sleep(0.12)
                except Exception as e:
                    print(f"{symbol}: {e}")
                    time.sleep(0.3)

            time.sleep(60)
        except Exception as e:
            print("Scanner cycle error:", e)
            time.sleep(15)

if __name__ == "__main__":
    main()

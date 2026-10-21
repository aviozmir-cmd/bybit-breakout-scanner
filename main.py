

import os
import time
import statistics
import requests
from datetime import datetime, timezone

BASE = "https://api.bytick.com"
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

MAX_COINS = 100
MIN_TURNOVER_24H = 200_000
MAX_TURNOVER_24H = 20_000_000
MIN_CANDLE_TURNOVER = 20_000
VOLUME_MULTIPLE = 2.5
MIN_BREAKOUT_PCT = 1.2
MAX_ABOVE_RESISTANCE_PCT = 6.0

session = requests.Session()


def api(path, params=None):
    response = session.get(
        BASE + path, params=params, timeout=15
    )
    response.raise_for_status()
    data = response.json()

    if data.get("retCode") != 0:
        raise RuntimeError(data.get("retMsg", "Bybit API error"))

    return data["result"]


def telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Telegram secrets are missing.")

    response = session.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={
            "chat_id": CHAT_ID,
            "text": message,
        },
        timeout=15,
    )
    response.raise_for_status()

    result = response.json()
    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the message.")


def get_symbols():
    instruments = api(
        "/v5/market/instruments-info",
        {"category": "spot", "limit": 1000},
    )["list"]

    tickers = api(
        "/v5/market/tickers",
        {"category": "spot"},
    )["list"]

    trading = {
        item["symbol"]
        for item in instruments
        if item.get("status") == "Trading"
    }

    excluded_suffixes = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
    stable_bases = {
        "USDC", "USDE", "DAI", "FDUSD", "TUSD",
        "USDP", "USDD", "EUR", "EURC",
    }

    candidates = []

    for ticker in tickers:
        symbol = ticker["symbol"]

        if not symbol.endswith("USDT") or symbol not in trading:
            continue

        if symbol.endswith(excluded_suffixes):
            continue

        if symbol[:-4] in stable_bases:
            continue

        try:
            turnover = float(ticker.get("turnover24h", 0))
            bid = float(ticker.get("bid1Price", 0))
            ask = float(ticker.get("ask1Price", 0))
        except (TypeError, ValueError):
            continue

        if not MIN_TURNOVER_24H <= turnover <= MAX_TURNOVER_24H:
            continue

        if bid <= 0 or ask <= 0:
            continue

        spread_pct = (ask - bid) / ((ask + bid) / 2) * 100

        if spread_pct > 0.5:
            continue

        candidates.append((turnover, symbol))

    candidates.sort(reverse=True)
    return [symbol for _, symbol in candidates[:MAX_COINS]]


def get_history(symbol):
    rows = api(
        "/v5/market/kline",
        {
            "category": "spot",
            "symbol": symbol,
            "interval": "5",
            "limit": 100,
        },
    )["list"]

    rows.sort(key=lambda row: int(row[0]))
    now_ms = int(time.time() * 1000)
    candles = []

    for row in rows:
        start = int(row[0])

        # Use only fully closed 5-minute candles.
        if start + 300_000 > now_ms:
            continue

        candles.append({
            "start": start,
            "high": float(row[2]),
            "close": float(row[4]),
            "volume": float(row[5]),
            "turnover": float(row[6]),
        })

    return candles


def check_signal(candles):
    if len(candles) < 22:
        return None

    current = candles[-1]
    previous = candles[-21:-1]

    resistance = max(candle["high"] for candle in previous)
    average_volume = statistics.mean(
        candle["volume"] for candle in previous
    )
    previous_close = candles[-2]["close"]

    if resistance <= 0 or average_volume <= 0 or previous_close <= 0:
        return None

    breakout_pct = (current["close"] / resistance - 1) * 100
    change_pct = (current["close"] / previous_close - 1) * 100
    volume_multiple = current["volume"] / average_volume

    if current["close"] <= resistance:
        return None
    if not MIN_BREAKOUT_PCT <= breakout_pct <= MAX_ABOVE_RESISTANCE_PCT:
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
        "candle_time": current["start"],
    }


def format_signal(symbol, signal):
    stamp = datetime.fromtimestamp(
        signal["candle_time"] / 1000,
        tz=timezone.utc,
    ).strftime("%Y-%m-%d %H:%M UTC")

    return (
        f"🚨 5m BREAKOUT: {symbol}\n"
        f"Price: {signal['price']:.8g} USDT\n"
        f"Resistance: {signal['resistance']:.8g}\n"
        f"Above resistance: {signal['breakout_pct']:.2f}%\n"
        f"5m change: {signal['change_pct']:.2f}%\n"
        f"Volume vs prior 20 candles: "
        f"{signal['volume_multiple']:.2f}x\n"
        f"Candle turnover: ${signal['turnover']:,.0f}\n"
        f"Candle closed: {stamp}\n"
        "Signal only — not a trading recommendation."
    )


def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in GitHub Secrets."
        )

    print("Starting one scan cycle.")

    telegram("🔎 Bybit scanner started a new scan cycle.")

    checked = 0
    errors = 0
    signals = 0

    try:
        symbols = get_symbols()
        print(f"Selected {len(symbols)} USDT spot pairs.")

    except Exception as error:
        print(f"Could not get symbol list: {error}")
        telegram(f"⚠️ Scanner could not load Bybit markets: {error}")
        raise

    for symbol in symbols:
        try:
            candles = get_history(symbol)
            checked += 1

            signal = check_signal(candles)

            if signal:
                telegram(format_signal(symbol, signal))
                signals += 1
                print(f"Signal sent: {symbol}")

        except Exception as error:
            errors += 1
            print(f"{symbol}: {error}")

        time.sleep(0.15)

    summary = (
        "📊 Bybit scan finished\n"
        f"Pairs selected: {len(symbols)}\n"
        f"Pairs checked: {checked}\n"
        f"Errors: {errors}\n"
        f"Breakouts found: {signals}\n"
        f"Finished: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
    )

    print(summary)
    telegram(summary)


if __name__ == "__main__":
    main()

import os
import time
import logging
from datetime import datetime, timezone

import requests
import pandas as pd

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

TG_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
GECKO_NEW_POOLS = "https://api.geckoterminal.com/api/v2/networks/{network}/new_pools"
GOPLUS_URL = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}"

# Aturan dari strategi Anda (ubah sesuai kebutuhan)
BTC_RSI_OVERBOUGHT = 70
XAU_LONG_BELOW = 3995

# Pengaturan scanner meme
MEME_MIN_MC = 50_000
MEME_MAX_MC = 500_000
MEME_MAX_AGE_HOURS = 1
GECKO_NETWORKS = ["eth", "base", "bsc", "solana"]
GOPLUS_CHAIN_IDS = {"eth": 1, "base": 8453, "bsc": 56}  # solana tidak dicek, dilewati

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

sent_keys = set()  # mencegah sinyal yang sama terkirim berulang


def send(text):
    try:
        r = requests.post(TG_URL, json={"chat_id": CHAT_ID, "text": text}, timeout=15)
        r.raise_for_status()
    except Exception:
        log.exception("Gagal kirim pesan Telegram")


def send_once(key, text):
    if key in sent_keys:
        return
    sent_keys.add(key)
    send(text)


# ---------- Data harga dari Binance ----------
def get_klines(symbol, interval, limit=100):
    r = requests.get(
        BINANCE_KLINES,
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=15,
    )
    r.raise_for_status()
    df = pd.DataFrame(r.json(), columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "qav", "trades", "tbb", "tbq", "ignore",
    ])
    df["close"] = df["close"].astype(float)
    return df


def ema(series, span):
    return series.ewm(span=span, adjust=False).mean()


def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


# ---------- 1. BTC SHORT ----------
def check_btc():
    h4 = get_klines("BTCUSDT", "4h")
    m15 = get_klines("BTCUSDT", "15m")

    price = m15["close"].iloc[-1]
    ema7_4h = ema(h4["close"], 7).iloc[-1]
    h4_close = h4["close"].iloc[-1]
    rsi_15m = rsi(m15["close"]).iloc[-1]

    # Aturan: harga 4 jam di bawah EMA7 DAN RSI 15 menit di atas 70 = SHORT
    if h4_close < ema7_4h and rsi_15m > BTC_RSI_OVERBOUGHT:
        candle = int(m15["open_time"].iloc[-1])
        send_once(
            f"BTC-SHORT-{candle}",
            f"🔴 BTC SHORT\n"
            f"Harga: {price:,.2f}\n"
            f"EMA7 4j: {ema7_4h:,.2f}\n"
            f"RSI 15m: {rsi_15m:.1f}\n"
            f"Trend 4j: BEARISH",
        )


# ---------- 2. XAU LONG (proxy PAXG) ----------
def check_xau():
    m15 = get_klines("PAXGUSDT", "15m")
    price = m15["close"].iloc[-1]

    if price <= XAU_LONG_BELOW:
        candle = int(m15["open_time"].iloc[-1])
        send_once(
            f"XAU-LONG-{candle}",
            f"🟢 XAU LONG (proxy PAXG)\n"
            f"Harga: {price:,.2f}\n"
            f"Ambang: {XAU_LONG_BELOW}",
        )


# ---------- 3. Scanner meme baru ----------
def is_safe(network, address):
    if network not in GOPLUS_CHAIN_IDS:
        return False  # Solana belum dicek, jadi dilewati
    try:
        url = GOPLUS_URL.format(chain_id=GOPLUS_CHAIN_IDS[network])
        r = requests.get(url, params={"contract_addresses": address}, timeout=15)
        r.raise_for_status()
        info = r.json().get("result", {}).get(address.lower())
        if not info:
            return False  # data tidak ada = anggap tidak aman
        if info.get("is_honeypot") != "0":
            return False
        if float(info.get("buy_tax") or 1) > 0.1:
            return False
        if float(info.get("sell_tax") or 1) > 0.1:
            return False
        return True
    except Exception:
        log.exception("Gagal cek GoPlus")
        return False


def scan_memes():
    now = datetime.now(timezone.utc)
    for network in GECKO_NETWORKS:
        try:
            r = requests.get(
                GECKO_NEW_POOLS.format(network=network),
                headers={"accept": "application/json"},
                timeout=15,
            )
            r.raise_for_status()
            pools = r.json().get("data", [])
        except Exception:
            log.exception("Gagal ambil pool baru %s", network)
            time.sleep(2)
            continue

        for pool in pools:
            attr = pool.get("attributes", {})
            address = attr.get("address")
            created_str = attr.get("pool_created_at")
            if not address or not created_str:
                continue

            created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            age_hours = (now - created).total_seconds() / 3600
            mc = float(attr.get("market_cap_usd") or attr.get("fdv_usd") or 0)

            if not (age_hours <= MEME_MAX_AGE_HOURS and MEME_MIN_MC <= mc <= MEME_MAX_MC):
                continue

            token_addr = pool["relationships"]["base_token"]["data"]["id"].split("_", 1)[1]
            symbol = attr.get("name", "").split(" / ")[0]

            if not is_safe(network, token_addr):
                continue

            send_once(
                f"MEME-{network}-{address}",
                f"💎 MEME BARU {network.upper()}\n"
                f"Token: {symbol}\n"
                f"MC: ${mc:,.0f}\n"
                f"Umur: {age_hours:.2f} jam\n"
                f"CA: {token_addr}\n"
                f"Chart: https://www.geckoterminal.com/{network}/pools/{address}",
            )
        time.sleep(2)  # hindari rate limit GeckoTerminal


# ---------- Loop utama ----------
def main():
    log.info("Bot jalan")
    send("✅ Bot sinyal aktif.")
    while True:
        for name, fn in [("BTC", check_btc), ("XAU", check_xau), ("MEME", scan_memes)]:
            try:
                fn()
            except Exception:
                log.exception("Error di %s", name)
        time.sleep(60)


if __name__ == "__main__":
    main()

import os
import json
import time
import statistics
import requests
from pathlib import Path
from datetime import datetime, timezone

BASE = "https://api.toobit.com"
ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()

# تنظیمات اولیه شکار
MAX_24H_CHANGE = 7.0
MIN_24H_VOLUME = 100000
MAX_CANDIDATES = 80
MIN_FIRST_SCORE = 3
MIN_CONFIRM_SCORE = 5
VOLUME_SPIKE = 1.8
WATCH_EXPIRY_MINUTES = 90
ALERT_COOLDOWN_MINUTES = 180

STABLECOINS = {
    "USDT", "USDC", "BUSD", "DAI", "FDUSD",
    "TUSD", "USDE", "USDS", "USD1", "PYUSD", "USDP"
}

S = requests.Session()
S.headers.update({"User-Agent": "Toobit-PreMove-Hunter/1.0"})


def api(path, params=None):
    url = BASE + path

    for attempt in range(3):
        try:
            r = S.get(url, params=params, timeout=20)
            r.raise_for_status()
            data = r.json()

            if isinstance(data, dict):
                code = data.get("code")
                if code not in (None, 200, "200"):
                    raise RuntimeError(
                        f"API error {code}: {data.get('msg', '')}"
                    )

            return data

        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def number(x, default=0):
    try:
        return float(x)
    except (ValueError, TypeError):
        return default


def change_pct(new, old):
    if old == 0:
        return 0
    return (new / old - 1) * 100


def load_state():
    if not STATE_FILE.exists():
        return {
            "watch": {},
            "last_alert": {},
            "last_oi": {}
        }

    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {
            "watch": {},
            "last_alert": {},
            "last_oi": {}
        }


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    tmp.replace(STATE_FILE)


def send_ntfy(title, message):
    if not NTFY_TOPIC:
        print("[WARNING] NTFY_TOPIC is not configured.")
        return

    url = f"https://ntfy.sh/{NTFY_TOPIC}"

    try:
        r = requests.post(
            url,
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": "high",
                "Tags": "mag,chart_with_upwards_trend"
            },
            timeout=20
        )
        r.raise_for_status()
        print("[NTFY] Notification sent.")

    except Exception as e:
        print("[NTFY ERROR]", e)


def get_contracts():
    data = api("/api/v1/exchangeInfo")
    contracts = data.get("contracts", [])

    result = []

    for c in contracts:
        symbol = str(c.get("symbol", ""))
        underlying = str(c.get("underlying", "")).upper()
        categories = [
            str(x).lower() for x in (c.get("categories") or [])
        ]

        if c.get("status") != "TRADING":
            continue

        if c.get("inverse") is not False:
            continue

        if c.get("marginToken") != "USDT":
            continue

        if c.get("quoteAsset") != "USDT":
            continue

        if "tradfi" in categories:
            continue

        if underlying in STABLECOINS:
            continue

        if not symbol.endswith("-SWAP-USDT"):
            continue

        result.append(symbol)

    return sorted(set(result))


def get_tickers():
    data = api(
        "/quote/v1/contract/ticker/24hr",
        {"realtimeInterval": "24h"}
    )

    if not isinstance(data, list):
        raise RuntimeError("Unexpected ticker response")

    return {
        str(x.get("s")): x
        for x in data
        if x.get("s")
    }


def get_klines(symbol):
    data = api(
        "/quote/v1/klines",
        {
            "symbol": symbol,
            "interval": "5m",
            "limit": 45
        }
    )

    rows = []

    for k in data:
        if not isinstance(k, list) or len(k) < 11:
            continue

        rows.append({
            "time": int(number(k[0])),
            "open": number(k[1]),
            "high": number(k[2]),
            "low": number(k[3]),
            "close": number(k[4]),
            "volume": number(k[5]),
            "quote_volume": number(k[7]),
            "taker_buy": number(k[9]),
            "taker_buy_quote": number(k[10])
        })

    return sorted(rows, key=lambda x: x["time"])


def get_open_interest(symbol):
    data = api(
        "/quote/v1/openInterest",
        {"symbol": symbol}
    )

    items = data.get("openInterestList", [])

    if not items:
        return 0

    return number(items[0].get("size"))


def get_funding():
    try:
        data = api("/api/v1/futures/fundingRate")

        if isinstance(data, list):
            return {
                x["symbol"]: number(x.get("rate"))
                for x in data
                if x.get("symbol")
            }

    except Exception as e:
        print("[WARNING] Funding unavailable:", e)

    return {}


def analyze_candles(rows):
    if len(rows) < 25:
        return None

    # آخرین کندل ممکن است هنوز باز باشد.
    current = rows[-1]

    # ۲۰ کندل قبل از کندل جاری
    previous = rows[-21:-1]

    volumes = [
        x["volume"] for x in previous
        if x["volume"] > 0
    ]

    if len(volumes) < 10:
        return None

    median_volume = statistics.median(volumes)

    volume_ratio = (
        current["volume"] / median_volume
        if median_volume else 0
    )

    high = max(x["high"] for x in previous)
    low = min(x["low"] for x in previous)

    midpoint = (high + low) / 2

    compression = (
        (high - low) / midpoint * 100
        if midpoint else 999
    )

    candle_range = current["high"] - current["low"]

    close_position = (
        (current["close"] - current["low"]) / candle_range
        if candle_range > 0 else 0.5
    )

    taker_ratio = (
        current["taker_buy"] / current["volume"]
        if current["volume"] > 0 else 0.5
    )

    age_minutes = max(
        0,
        (time.time() * 1000 - current["time"]) / 60000
    )

    return {
        "current": current,
        "high": high,
        "low": low,
        "volume_ratio": volume_ratio,
        "compression": compression,
        "close_position": close_position,
        "taker_ratio": taker_ratio,
        "age_minutes": age_minutes,
        "forming": age_minutes < 5
    }


def score_candidate(symbol, ticker, features, oi, previous_oi, funding):
    price = number(ticker.get("c"))
    change24 = number(ticker.get("pcp"))
    volume24 = number(ticker.get("qv"))

    if price <= 0:
        return None

    long_score = 0
    short_score = 0

    long_reasons = []
    short_reasons = []

    # ۱. هنوز حرکت اصلی ۲۴ ساعته رخ نداده باشد
    if abs(change24) <= MAX_24H_CHANGE:
        long_score += 1
        short_score += 1
        long_reasons.append("تغییر ۲۴ ساعته هنوز محدود است")
        short_reasons.append("تغییر ۲۴ ساعته هنوز محدود است")

    # ۲. افزایش غیرعادی حجم
    if features["volume_ratio"] >= VOLUME_SPIKE:
        long_score += 2
        short_score += 2

        reason = (
            f"حجم کندل جاری {features['volume_ratio']:.2f} "
            "برابر میانه ۲۰ کندل قبلی است"
        )

        long_reasons.append(reason)
        short_reasons.append(reason)

    # ۳. فشردگی قیمت
    if features["compression"] <= 3:
        long_score += 1
        short_score += 1

        reason = (
            f"فشردگی محدوده اخیر: "
            f"{features['compression']:.2f}%"
        )

        long_reasons.append(reason)
        short_reasons.append(reason)

    # ۴. نزدیک شدن به سقف یا کف محدوده
    distance_high = change_pct(price, features["high"])
    distance_low = change_pct(price, features["low"])

    if -2 <= distance_high <= 0.8:
        long_score += 1
        long_reasons.append("قیمت نزدیک سقف محدوده اخیر است")

    if -0.8 <= distance_low <= 2:
        short_score += 1
        short_reasons.append("قیمت نزدیک کف محدوده اخیر است")

    # ۵. فشار خرید یا فروش
    if features["taker_ratio"] >= 0.58:
        long_score += 1
        long_reasons.append("غلبه خرید تهاجمی")

    elif features["taker_ratio"] <= 0.42:
        short_score += 1
        short_reasons.append("غلبه فروش تهاجمی")

    # ۶. افزایش Open Interest بین دو اسکن
    oi_change = None

    if previous_oi > 0 and oi > 0:
        oi_change = change_pct(oi, previous_oi)

        if oi_change >= 0.5:
            long_score += 1
            short_score += 1

            reason = f"افزایش Open Interest: {oi_change:.2f}%"

            long_reasons.append(reason)
            short_reasons.append(reason)

    # ۷. وضعیت کندل در حال تشکیل
    candle = features["current"]

    if candle["close"] > candle["open"]:
        if features["close_position"] >= 0.65:
            long_score += 1
            long_reasons.append(
                "کندل جاری صعودی و نزدیک سقف خودش است"
            )

    elif candle["close"] < candle["open"]:
        if features["close_position"] <= 0.35:
            short_score += 1
            short_reasons.append(
                "کندل جاری نزولی و نزدیک کف خودش است"
            )

    if long_score > short_score:
        direction = "LONG"
        score = long_score
        reasons = long_reasons

    elif short_score > long_score:
        direction = "SHORT"
        score = short_score
        reasons = short_reasons

    else:
        direction = "MIXED"
        score = max(long_score, short_score)
        reasons = []

    # سطوح پایش بر اساس محدوده اخیر؛ نه سیگنال تضمینی ورود
    volatility = max(
        (features["high"] - features["low"]) / 20,
        price * 0.003
    )

    if direction == "LONG":
        invalidation = min(
            features["low"],
            price - volatility
        )

        targets = [
            price + volatility,
            price + 1.8 * volatility,
            price + 2.7 * volatility
        ]

    elif direction == "SHORT":
        invalidation = max(
            features["high"],
            price + volatility
        )

        targets = [
            price - volatility,
            price - 1.8 * volatility,
            price - 2.7 * volatility
        ]

    else:
        invalidation = None
        targets = []

    return {
        "symbol": symbol,
        "direction": direction,
        "score": score,
        "price": price,
        "change24": change24,
        "volume24": volume24,
        "volume_ratio": features["volume_ratio"],
        "oi": oi,
        "oi_change": oi_change,
        "funding": funding,
        "forming": features["forming"],
        "candle_age": features["age_minutes"],
        "reasons": reasons,
        "invalidation": invalidation,
        "targets": targets
    }


def format_price(value):
    if value is None:
        return "N/A"
    return f"{value:.10g}"


def make_alert(item):
    candle_status = (
        "کندل در حال تشکیل"
        if item["forming"]
        else "آخرین کندل دریافتی"
    )

    lines = [
        "🚨 TOOBIT PRE-MOVE HUNTER",
        "",
        f"ارز: {item['symbol']}",
        f"جهت احتمالی: {item['direction']}",
        f"امتیاز: {item['score']}",
        f"قیمت: {format_price(item['price'])}",
        f"تغییر ۲۴ ساعته: {item['change24']:.2f}%",
        f"حجم نسبی کندل: {item['volume_ratio']:.2f}x",
        f"Open Interest: {format_price(item['oi'])}",
        f"Funding: {item['funding']:.6f}",
        f"وضعیت: {candle_status}",
        f"سن کندل: {item['candle_age']:.1f} دقیقه",
        "",
        "دلایل شکار:"
    ]

    for reason in item["reasons"]:
        lines.append("• " + reason)

    if item["invalidation"] is not None:
        lines += [
            "",
            f"سطح ابطال سناریو: {format_price(item['invalidation'])}",
            "اهداف محاسباتی پایش:"
        ]

        for i, target in enumerate(item["targets"], 1):
            lines.append(f"هدف {i}: {format_price(target)}")

    lines += [
        "",
        "این هشدار پژوهشی است، نه تضمین حرکت یا توصیه ورود.",
        "هیچ معامله‌ای به صورت خودکار انجام نمی‌شود."
    ]

    return "\n".join(lines)


def main():
    print("=" * 45)
    print("TOOBIT CRYPTO PRE-MOVE HUNTER")
    print(datetime.now(timezone.utc).isoformat())
    print("=" * 45)

    state = load_state()

    state.setdefault("watch", {})
    state.setdefault("last_alert", {})
    state.setdefault("last_oi", {})

    # دریافت بازارهای مجاز
    contracts = get_contracts()
    print("Eligible crypto contracts:", len(contracts))

    # دریافت تیکرهای بازار
    tickers = get_tickers()

    candidates = []

    for symbol in contracts:
        ticker = tickers.get(symbol)

        if not ticker:
            continue

        change = abs(number(ticker.get("pcp")))
        volume = number(ticker.get("qv"))

        if change > MAX_24H_CHANGE:
            continue

        if volume < MIN_24H_VOLUME:
            continue

        candidates.append((volume, symbol, ticker))

    candidates.sort(reverse=True)

    selected = {
        symbol: ticker
        for _, symbol, ticker in candidates[:MAX_CANDIDATES]
    }

    # ارزهای شکارشده قبلی نیز حتماً دوباره بررسی می‌شوند.
    for symbol in state["watch"]:
        if symbol in tickers:
            selected[symbol] = tickers[symbol]

    print("Initial candidates:", len(candidates))
    print("Symbols to analyze:", len(selected))

    funding_map = get_funding()

    now = time.time()
    new_watch = {}
    alerts = []

    for symbol, ticker in selected.items():

        try:
            rows = get_klines(symbol)
            features = analyze_candles(rows)

            if features is None:
                continue

            oi = get_open_interest(symbol)
            previous_oi = number(
                state["last_oi"].get(symbol)
            )

            item = score_candidate(
                symbol,
                ticker,
                features,
                oi,
                previous_oi,
                funding_map.get(symbol, 0)
            )

            if not item:
                continue

            state["last_oi"][symbol] = oi

            old = state["watch"].get(symbol)

            old_age = (
                (now - number(old.get("first_seen", now))) / 60
                if old else 0
            )

            # مرحله اول: شکار اولیه و ذخیره
            if (
                item["score"] >= MIN_FIRST_SCORE
                and item["direction"] != "MIXED"
            ):

                new_watch[symbol] = {
                    "first_seen": (
                        old.get("first_seen", now)
                        if old else now
                    ),
                    "last_seen": now,
                    "direction": item["direction"],
                    "best_score": max(
                        item["score"],
                        number(old.get("best_score")) if old else 0
                    )
                }

                # مرحله دوم: فقط در اسکن بعدی اجازه تأیید دارد
                if old:

                    last_alert = number(
                        state["last_alert"].get(symbol)
                    )

                    cooldown = (
                        (now - last_alert) / 60
                        if last_alert else 999999
                    )

                    same_direction = (
                        old.get("direction") == item["direction"]
                    )

                    if (
                        same_direction
                        and item["score"] >= MIN_CONFIRM_SCORE
                        and cooldown >= ALERT_COOLDOWN_MINUTES
                    ):
                        alerts.append(item)
                        state["last_alert"][symbol] = now

            # اگر یک اسکن ضعیف شد، مدتی در فهرست پایش بماند
            elif old and old_age <= WATCH_EXPIRY_MINUTES:
                old["last_seen"] = now
                new_watch[symbol] = old

            print(
                symbol,
                item["direction"],
                "score:",
                item["score"]
            )

        except Exception as e:
            print("[SYMBOL ERROR]", symbol, str(e))

        # مکث کوتاه برای رعایت محدودیت درخواست API
        time.sleep(0.12)

    state["watch"] = new_watch
    save_state(state)

    print("Active watchlist:", len(new_watch))
    print("Confirmed alerts:", len(alerts))

    for item in alerts:
        message = make_alert(item)

        print("\n" + message + "\n")

        send_ntfy(
            f"SHARKED: {item['symbol']} {item['direction']}",
            message
        )


if __name__ == "__main__":
    main()

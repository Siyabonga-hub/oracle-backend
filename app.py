import os
import time
import requests
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

def after_request_handler(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

app.after_request(after_request_handler)

@app.route("/")
def home():
    return jsonify({"status": "Oracle is alive", "version": "1.0"})

# ── CHAT (ICT analysis) ───────────────────────────────────────────────────────
@app.route("/chat", methods=["POST", "OPTIONS"])
def chat():
    if request.method == "OPTIONS":
        return Response(status=200)
    try:
        data     = request.json or {}
        messages = data.get("messages", [])
        identity = data.get("identity", "You are an ICT trading intelligence. JSON only.")

        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": "Bearer " + GROQ_API_KEY, "Content-Type": "application/json"},
            json={"model": "llama-3.1-8b-instant", "messages": [{"role": "system", "content": identity}] + messages, "max_tokens": 800, "temperature": 0.7},
            timeout=30
        )
        result = r.json()
        if "choices" not in result:
            return jsonify({"error": str(result)}), 500
        return jsonify({"reply": result["choices"][0]["message"]["content"], "status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── MARKET DATA ───────────────────────────────────────────────────────────────
SYMBOL_MAP = {
    "BTC": "BTCUSDT",
    "NAS100": "NQ=F", "US30": "YM=F", "SPX": "ES=F",
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "JPY=X",
    "USDCHF": "CHF=X", "AUDUSD": "AUDUSD=X", "USDCAD": "CAD=X"
}
INTERVAL_MAP = {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "1h", "1d": "1d"}
RANGE_MAP    = {"5m": "2d", "15m": "5d", "1h": "30d", "4h": "60d", "1d": "1y"}

@app.route("/market-data", methods=["GET", "OPTIONS"])
def market_data():
    if request.method == "OPTIONS":
        return Response(status=200)
    try:
        instrument = request.args.get("instrument", "BTC").upper()
        interval   = request.args.get("interval", "5m")

        if instrument == "BTC":
            r = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": "BTCUSDT", "interval": INTERVAL_MAP.get(interval, "5m"), "limit": 60},
                timeout=10
            )
            data = r.json()
            candles = [{"t": c[0], "o": float(c[1]), "h": float(c[2]), "l": float(c[3]), "c": float(c[4]), "v": float(c[5])} for c in data]
            return jsonify({"candles": candles, "instrument": instrument, "interval": interval})

        yahoo_sym = SYMBOL_MAP.get(instrument)
        if not yahoo_sym:
            return jsonify({"error": "Unknown instrument"}), 400

        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/" + yahoo_sym,
            params={"interval": INTERVAL_MAP.get(interval, "5m"), "range": RANGE_MAP.get(interval, "2d")},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15
        )
        data   = r.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return jsonify({"error": "No data from Yahoo"}), 404

        res    = result[0]
        ts     = res.get("timestamp", [])
        quote  = res.get("indicators", {}).get("quote", [{}])[0]
        opens  = quote.get("open", [])
        highs  = quote.get("high", [])
        lows   = quote.get("low", [])
        closes = quote.get("close", [])
        vols   = quote.get("volume", [])

        candles = []
        for i in range(len(ts)):
            if closes[i] is None:
                continue
            candles.append({"t": ts[i]*1000, "o": opens[i] or closes[i], "h": highs[i] or closes[i],
                            "l": lows[i] or closes[i], "c": closes[i], "v": vols[i] or 0})
        return jsonify({"candles": candles, "instrument": instrument, "interval": interval})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── POLYMARKET ODDS PROXY ─────────────────────────────────────────────────────
@app.route("/polymarket/odds", methods=["GET", "OPTIONS"])
def polymarket_odds():
    if request.method == "OPTIONS":
        return Response(status=200)
    try:
        now          = int(time.time())
        window_start = now - (now % 300)

        for offset in [0, -300, -600]:
            slug = "btc-updown-5m-{}".format(window_start + offset)
            r    = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"slug": slug},
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                timeout=10
            )
            data    = r.json()
            markets = data if isinstance(data, list) else data.get("results", data.get("markets", []))
            if markets:
                break

        if not markets:
            return jsonify({"error": "Market not found", "up": 50, "dn": 50}), 404

        market = markets[0]
        tokens = market.get("tokens", market.get("outcomes", []))
        up_price, dn_price = 50.0, 50.0

        for t in tokens:
            name  = (t.get("outcome") or t.get("name") or "").lower()
            price = float(t.get("price") or t.get("probability") or 0)
            if price <= 1:
                price = price * 100
            if "higher" in name or name == "up":
                up_price = round(price, 1)
            elif "lower" in name or name == "down":
                dn_price = round(price, 1)

        return jsonify({
            "slug":       slug,
            "up":         up_price,
            "dn":         dn_price,
            "up_mult":    round(100 / up_price, 2) if up_price > 0 else 0,
            "dn_mult":    round(100 / dn_price, 2) if dn_price > 0 else 0,
            "market_url": "https://polymarket.com/event/" + slug,
            "status":     "ok"
        })

    except Exception as e:
        return jsonify({"error": str(e), "up": 50, "dn": 50}), 500

# ── POLYMARKET WEATHER MARKETS ────────────────────────────────────────────────
@app.route("/polymarket/weather", methods=["GET", "OPTIONS"])
def polymarket_weather():
    if request.method == "OPTIONS":
        return Response(status=200)
    try:
        markets = []
        for search_term in ["temperature", "weather", "rain", "snow"]:
            r = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"search": search_term, "active": "true", "limit": 10},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10
            )
            data = r.json()
            batch = data if isinstance(data, list) else data.get("results", data.get("markets", []))
            for m in batch:
                if m.get("slug") not in [x.get("slug") for x in markets]:
                    markets.append(m)
            if len(markets) >= 15:
                break

        result = []
        for m in markets[:15]:
            tokens = m.get("tokens", m.get("outcomes", []))
            outcomes = []
            for t in tokens:
                price = float(t.get("price") or t.get("probability") or 0)
                if price <= 1:
                    price = price * 100
                outcomes.append({
                    "name":  t.get("outcome") or t.get("name") or "—",
                    "price": round(price, 1),
                    "mult":  round(100 / price, 2) if price > 0 else 0
                })
            result.append({
                "title":    m.get("question") or m.get("title") or m.get("name") or "Weather Market",
                "slug":     m.get("slug", ""),
                "end_date": m.get("endDate") or m.get("end_date") or "",
                "volume":   float(m.get("volume") or m.get("volumeNum") or 0),
                "outcomes": outcomes,
                "url":      "https://polymarket.com/event/" + (m.get("slug") or "")
            })

        result.sort(key=lambda x: x["volume"], reverse=True)
        return jsonify({"markets": result, "count": len(result), "status": "ok"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── WEATHER FORECAST (Open-Meteo, free, no API key) ───────────────────────────
@app.route("/weather/forecast", methods=["GET", "OPTIONS"])
def weather_forecast():
    if request.method == "OPTIONS":
        return Response(status=200)
    try:
        lat  = request.args.get("lat", "40.7128")
        lon  = request.args.get("lon", "-74.0060")
        city = request.args.get("city", "New York")

        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude":  lat,
                "longitude": lon,
                "hourly":    "temperature_2m,precipitation_probability,precipitation,windspeed_10m,weathercode",
                "daily":     "temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,windspeed_10m_max",
                "temperature_unit": "fahrenheit",
                "windspeed_unit":   "mph",
                "forecast_days":    7,
                "timezone":         "auto"
            },
            timeout=10
        )
        data = r.json()

        daily  = data.get("daily", {})
        hourly = data.get("hourly", {})

        forecast = []
        dates = daily.get("time", [])
        for i in range(len(dates)):
            forecast.append({
                "date":          dates[i],
                "temp_max":      daily.get("temperature_2m_max", [None]*10)[i],
                "temp_min":      daily.get("temperature_2m_min", [None]*10)[i],
                "precip_sum":    daily.get("precipitation_sum", [None]*10)[i],
                "precip_prob":   daily.get("precipitation_probability_max", [None]*10)[i],
                "wind_max":      daily.get("windspeed_10m_max", [None]*10)[i]
            })

        return jsonify({
            "city":     city,
            "lat":      lat,
            "lon":      lon,
            "forecast": forecast,
            "status":   "ok"
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

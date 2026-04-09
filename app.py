from flask import Flask, request, jsonify
from flask_cors import CORS
import yfinance as yf
import os
from groq import Groq

app = Flask(__name__)
CORS(app)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
client = Groq(api_key=GROQ_API_KEY)

SYMBOLS = {
    "NQ=F":  "NAS100 (E-mini Futures)",
    "GC=F":  "XAUUSD (Gold Futures)",
    "ES=F":  "S&P500 (E-mini Futures)",
    "YM=F":  "US30 (Dow Futures)",
}

# ─── YAHOO FINANCE DATA ──────────────────────────────────────────────────────

def fetch_candles(symbol, interval, period):
    """
    interval: '5m', '1h'
    period:   '1d' for 5m | '5d' for 1h
    yfinance 5m data available up to 60 days back.
    """
    ticker = yf.Ticker(symbol)
    df = ticker.history(interval=interval, period=period)

    if df.empty:
        raise ValueError(f"No data for {symbol} at {interval} — market may be closed")

    candles = []
    for _, row in df.iterrows():
        candles.append({
            "open":  round(float(row["Open"]),  4),
            "high":  round(float(row["High"]),  4),
            "low":   round(float(row["Low"]),   4),
            "close": round(float(row["Close"]), 4),
            "vol":   round(float(row["Volume"]), 2),
        })
    return candles

# ─── FVG DETECTION ───────────────────────────────────────────────────────────

def detect_fvgs(candles):
    """
    Bullish FVG: candle[i-2].high < candle[i].low
    Bearish FVG: candle[i-2].low  > candle[i].high
    Returns most recent 3 unfilled FVGs.
    """
    fvgs = []
    for i in range(2, len(candles)):
        c0 = candles[i - 2]
        c2 = candles[i]

        if c0["high"] < c2["low"]:
            fvgs.append({
                "type":     "bullish",
                "top":      c2["low"],
                "bottom":   c0["high"],
                "midpoint": round((c2["low"] + c0["high"]) / 2, 4),
                "idx":      i,
            })
        elif c0["low"] > c2["high"]:
            fvgs.append({
                "type":     "bearish",
                "top":      c0["low"],
                "bottom":   c2["high"],
                "midpoint": round((c0["low"] + c2["high"]) / 2, 4),
                "idx":      i,
            })

    current_price = candles[-1]["close"]
    unfilled = []
    for fvg in fvgs[-15:]:
        if fvg["type"] == "bullish" and current_price > fvg["midpoint"]:
            unfilled.append(fvg)
        elif fvg["type"] == "bearish" and current_price < fvg["midpoint"]:
            unfilled.append(fvg)

    return unfilled[-3:]

# ─── HTF BIAS ────────────────────────────────────────────────────────────────

def get_htf_bias(candles_1h):
    c = candles_1h[-24:]
    if len(c) < 10:
        return {
            "bias":          "NEUTRAL",
            "reason":        "Insufficient 1H data",
            "current_price": c[-1]["close"] if c else 0,
        }

    mid         = len(c) // 2
    recent_high = max(x["high"] for x in c[mid:])
    prev_high   = max(x["high"] for x in c[:mid])
    recent_low  = min(x["low"]  for x in c[mid:])
    prev_low    = min(x["low"]  for x in c[:mid])

    if recent_high > prev_high and recent_low > prev_low:
        bias   = "BULLISH"
        reason = "1H: Higher High + Higher Low confirmed — bullish BOS"
    elif recent_high < prev_high and recent_low < prev_low:
        bias   = "BEARISH"
        reason = "1H: Lower High + Lower Low confirmed — bearish BOS"
    else:
        bias   = "NEUTRAL"
        reason = "1H: Mixed structure — no clean BOS, stand aside"

    return {"bias": bias, "reason": reason, "current_price": c[-1]["close"]}

# ─── KELLY CRITERION ─────────────────────────────────────────────────────────

def kelly_size(win_rate=0.55, rr=2.0):
    kelly = win_rate - (1 - win_rate) / rr
    half_kelly = kelly / 2
    # Cap at 1.5% for indices/gold — more conservative than crypto
    return round(min(half_kelly, 0.015) * 100, 2)

# ─── ORACLE GROQ AGENT ───────────────────────────────────────────────────────

def run_oracle_agent(symbol, name, fvgs, htf_bias, current_price):
    fvg_text = "\n".join([
        f"  [{f['type'].upper()} FVG] top={f['top']} | bottom={f['bottom']} | midpoint={f['midpoint']}"
        for f in fvgs
    ]) if fvgs else "  No unfilled FVGs on 5M"

    prompt = f"""You are ORACLE — an elite ICT trader specialising in {name}.
Your only strategy: Fair Value Gap (FVG) entries confirmed by 1H HTF bias.
No other setups. Strict discipline.

INSTRUMENT: {name}
CURRENT PRICE: {current_price}

HTF BIAS (1H): {htf_bias['bias']}
Structure note: {htf_bias['reason']}

UNFILLED 5M FVGs:
{fvg_text}

TASK:
1. Is there a 5M FVG aligned with the {htf_bias['bias']} HTF bias?
   - BULLISH: look for BULLISH FVG below price (retrace fill)
   - BEARISH: look for BEARISH FVG above price (retrace fill)
2. If YES, respond with:
   ENTRY: [FVG midpoint]
   STOP:  [just beyond FVG — use 10pt buffer for NAS100, $2 for Gold]
   TP1:   [1:2 R:R]
   TP2:   [1:3 R:R]
   CONFLUENCE: LOW / MEDIUM / HIGH
   REASON: [one sentence of ICT reasoning]
3. If NO, say NO SETUP and briefly explain why.

No filler. No hedging. Discipline over FOMO. No trade is a valid trade."""

    response = client.chat.completions.create(
        model="llama3-70b-8192",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=350,
        temperature=0.15,
    )
    return response.choices[0].message.content.strip()

# ─── ROUTES ──────────────────────────────────────────────────────────────────

@app.route("/analyze", methods=["POST"])
def analyze():
    data   = request.json or {}
    symbol = data.get("symbol", "NQ=F")

    if symbol not in SYMBOLS:
        return jsonify({"error": f"Unsupported symbol: {symbol}"}), 400

    try:
        candles_5m = fetch_candles(symbol, "5m", "1d")
        candles_1h = fetch_candles(symbol, "1h", "5d")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if len(candles_5m) < 10 or len(candles_1h) < 10:
        return jsonify({"error": "Insufficient data — market may be closed or pre-market"}), 500

    fvgs     = detect_fvgs(candles_5m)
    htf_bias = get_htf_bias(candles_1h)
    kelly    = kelly_size()

    try:
        analysis = run_oracle_agent(symbol, SYMBOLS[symbol], fvgs, htf_bias, htf_bias["current_price"])
    except Exception as e:
        analysis = f"Agent error: {str(e)}"

    return jsonify({
        "symbol":        symbol,
        "name":          SYMBOLS[symbol],
        "current_price": htf_bias["current_price"],
        "htf_bias":      htf_bias,
        "fvgs":          fvgs,
        "kelly_pct":     kelly,
        "analysis":      analysis,
        "candles":       candles_5m[-80:],
    })

@app.route("/symbols", methods=["GET"])
def symbols():
    return jsonify([{"symbol": k, "name": v} for k, v in SYMBOLS.items()])

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "oracle online", "instruments": list(SYMBOLS.keys())})

if __name__ == "__main__":
    app.run(debug=True, port=5000)

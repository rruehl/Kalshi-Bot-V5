"""
dashboard_v5.py — v5.2.0
=========================
Flask dashboard for production_bot_v5.
Mobile-first compact layout based on v4 reference design.

PnL chart: cumulative realized PnL only.
  - Built from pnl_this_trade column on PAYOUT/SETTLE rows.
  - Never plots entry cost — chart only moves when a trade closes.
  - Dynamic green/red gradient crossing zero.
  - 1H / 6H / 24H / ALL time filters.
"""

import glob
import json
import os
import time
from datetime import datetime, timezone

import pandas as pd
import pytz
from flask import Flask, render_template_string

app = Flask(__name__)

CSV_FILE      = "production_log_v5.csv"
START_BALANCE = 1000.0

EVENT_COLORS = {
    "PAPER_BUY":       "#00e676",
    "LIVE_BUY":        "#00e676",
    "PAYOUT":          "#00e676",
    "SETTLE_VERIFIED": "#448aff",
    "SETTLE":          "#ff5252",
    "ERROR":           "#ff5252",
    "SYSTEM":          "#888",
}

def _event_color(event: str) -> str:
    for k, c in EVENT_COLORS.items():
        if k in event:
            return c
    return "#888"


HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="10">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root {
            --bg:     #111;
            --card:   #1a1a1a;
            --border: #2a2a2a;
            --text:   #e0e0e0;
            --muted:  #666;
            --green:  #00e676;
            --red:    #ff5252;
            --yellow: #ffd740;
            --blue:   #448aff;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, system-ui, sans-serif;
            background: var(--bg); color: var(--text);
            padding: 10px; font-size: 13px;
        }
        .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 10px; }
        .card-title { font-size: 0.65rem; color: var(--muted); text-transform: uppercase; letter-spacing: 1.5px; margin-bottom: 6px; }
        .big-val { font-size: 1.7rem; font-weight: 700; font-variant-numeric: tabular-nums; line-height: 1.1; }
        .sub { font-size: 0.75rem; color: var(--muted); margin-top: 3px; }
        .green  { color: var(--green); }
        .red    { color: var(--red); }
        .yellow { color: var(--yellow); }
        .blue   { color: var(--blue); }
        .header { display: flex; justify-content: space-between; align-items: center; padding: 8px 12px; background: var(--card); border: 1px solid var(--border); border-radius: 8px; margin-bottom: 10px; }
        .header-title { font-size: 0.9rem; font-weight: 700; letter-spacing: 2px; }
        .header-right { text-align: right; font-size: 0.7rem; color: var(--muted); line-height: 1.6; }
        .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 5px; vertical-align: middle; }
        .dot-green  { background: var(--green); box-shadow: 0 0 6px var(--green); }
        .dot-yellow { background: var(--yellow); }
        .dot-red    { background: var(--red); }
        .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 10px; }
        .sig-pill { display: inline-block; padding: 2px 10px; border-radius: 4px; font-size: 0.75rem; font-weight: 700; letter-spacing: 1px; }
        .sig-buy  { background: rgba(0,230,118,0.15); color: var(--green); border: 1px solid var(--green); }
        .sig-sell { background: rgba(255,82,82,0.15);  color: var(--red);   border: 1px solid var(--red); }
        .sig-idle { background: rgba(255,215,64,0.15); color: var(--yellow); border: 1px solid var(--yellow); }
        .hbar { height: 3px; background: var(--border); border-radius: 2px; margin-top: 5px; }
        .hbar-fill { height: 100%; border-radius: 2px; }
        .chart-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
        .chart-btns { display: flex; gap: 4px; }
        .btn-t { background: var(--border); border: 1px solid #333; color: var(--muted); padding: 3px 7px; border-radius: 4px; cursor: pointer; font-size: 0.7rem; font-family: inherit; }
        .btn-t.active { background: var(--green); color: #000; font-weight: 700; border-color: var(--green); }
        .chart-wrap { position: relative; height: 200px; }
        table { width: 100%; border-collapse: collapse; font-size: 0.78rem; }
        th { text-align: left; color: var(--muted); border-bottom: 1px solid var(--border); padding: 5px 0; font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.8px; }
        td { padding: 7px 0; border-bottom: 1px solid #1f1f1f; vertical-align: middle; }
        td:last-child { color: var(--muted); font-size: 0.75rem; word-break: break-word; }
    </style>
</head>
<body>

<div class="header">
    <div>
        <div class="header-title">
            <span class="dot {{ 'dot-green' if is_active and not ob_stale else 'dot-yellow' if is_active else 'dot-red' }}"></span>
            PRICE STALKER <span style="color:#444;">v5</span>
        </div>
        <div style="font-size:0.65rem; color:var(--muted); margin-top:2px;">
            {{ mode }} &nbsp;·&nbsp; {{ 'Online' if is_active else 'Offline' }}{{ ' · Stale OB' if ob_stale else '' }}
        </div>
    </div>
    <div class="header-right">
        {{ last_update }} CST<br>
        <span style="color:#444;">refresh 10s</span>
    </div>
</div>

<div class="grid-2">
    <div class="card">
        <div class="card-title">Realized P&amp;L</div>
        <div class="big-val {{ 'green' if pnl >= 0 else 'red' }}">${{ "{:+.2f}".format(pnl) }}</div>
        <div class="sub">Bank: ${{ "{:,.2f}".format(balance) }}</div>
    </div>
    <div class="card">
        <div class="card-title">UT Bot Signal</div>
        <div style="margin:4px 0;">
            <span class="sig-pill {{ 'sig-buy' if ut_signal == 'buy' else 'sig-sell' if ut_signal == 'sell' else 'sig-idle' }}">
                {{ ut_signal | upper if ut_signal else 'IDLE' }}
            </span>
        </div>
        <div class="sub">Age: {{ signal_age }}m &nbsp;/&nbsp; Limit: {{ stalk_limit }}m</div>
        <div class="sub">Stop: ${{ "{:.2f}".format(ut_stop) }} &nbsp; ATR: {{ "{:.2f}".format(ut_atr) }}</div>
    </div>
</div>

<div class="grid-2">
    <div class="card">
        <div class="card-title">Win Rate</div>
        <div class="big-val">{{ "{:.1f}".format(win_rate) }}%</div>
        <div class="sub">{{ wins }}W / {{ losses }}L / {{ total }} settled</div>
        <div class="hbar">
            <div class="hbar-fill" style="width:{{ win_rate }}%; background:{{ 'var(--green)' if win_rate >= 67 else 'var(--yellow)' if win_rate >= 50 else 'var(--red)' }};"></div>
        </div>
    </div>
    <div class="card">
        <div class="card-title">Trade Quality</div>
        <div class="sub" style="line-height:1.9;">
            Avg entry: <span class="blue">{{ avg_entry }}¢</span><br>
            Avg spread: {{ avg_spread }}¢<br>
            Avg sig age: {{ avg_signal_age }}m<br>
            Avg PnL: <span class="{{ 'green' if avg_pnl >= 0 else 'red' }}">${{ "{:+.4f}".format(avg_pnl) }}</span>
        </div>
    </div>
</div>

<div class="grid-2">
    <div class="card">
        <div class="card-title">Market</div>
        <div class="sub" style="line-height:1.9;">
            <span style="color:var(--text); font-size:0.8rem;">{{ ticker }}</span><br>
            Expires: <span class="yellow">{{ "{:.1f}".format(time_left) }}m</span><br>
            Strike: ${{ "{:,.0f}".format(strike) }}<br>
            OBI: <span class="{{ 'green' if obi > 0.2 else 'red' if obi < -0.2 else '' }}">{{ "{:+.3f}".format(obi) }}</span>
        </div>
    </div>
    <div class="card">
        <div class="card-title">Order Book</div>
        <div class="sub" style="line-height:1.9;">
            Yes bid: <span class="green">{{ yes_bid }}¢</span> ask: {{ yes_ask }}¢<br>
            No bid: <span class="red">{{ no_bid }}¢</span> ask: {{ no_ask }}¢<br>
            Yes liq: {{ yes_liq }}<br>
            No liq: {{ no_liq }}
        </div>
    </div>
</div>

<div class="card">
    <div class="chart-header">
        <div class="card-title" style="margin:0;">Realized P&amp;L Curve</div>
        <div class="chart-btns">
            <button class="btn-t" onclick="filterChart(1,this)">1H</button>
            <button class="btn-t" onclick="filterChart(6,this)">6H</button>
            <button class="btn-t" onclick="filterChart(24,this)">24H</button>
            <button class="btn-t active" onclick="filterChart(0,this)">ALL</button>
        </div>
    </div>
    <div class="chart-wrap"><canvas id="pnlChart"></canvas></div>
</div>

<div class="card">
    <div class="card-title">Activity Log</div>
    <table>
        <thead>
            <tr>
                <th style="width:18%;">Time</th>
                <th style="width:22%;">Event</th>
                <th>Detail</th>
            </tr>
        </thead>
        <tbody>
            {% for row in logs %}
            <tr>
                <td style="color:var(--muted);">{{ row.time }}</td>
                <td><span style="color:{{ row.color }};">{{ row.event }}</span></td>
                <td>{{ row.msg }}</td>
            </tr>
            {% endfor %}
            {% if not logs %}
            <tr><td colspan="3" style="text-align:center;padding:15px;color:var(--muted);">No activity yet</td></tr>
            {% endif %}
        </tbody>
    </table>
</div>

<script>
const allLabels     = {{ chart_labels | tojson }};
const allData       = {{ chart_data | tojson }};
const allTimestamps = {{ chart_timestamps | tojson }};
const ctx = document.getElementById('pnlChart').getContext('2d');
let chart;

function getGradient(cx, chartArea, scales) {
    if (!chartArea || !scales || !scales.y) return '#00e676';
    const zeroPx = scales.y.getPixelForValue(0);
    const t = chartArea.top, b = chartArea.bottom, h = b - t;
    if (h <= 0) return '#00e676';
    const zr = Math.max(0, Math.min(1, (zeroPx - t) / h));
    const g = cx.createLinearGradient(0, t, 0, b);
    g.addColorStop(0,  '#00e676');
    g.addColorStop(zr, '#00e676');
    g.addColorStop(zr, '#ff5252');
    g.addColorStop(1,  '#ff5252');
    return g;
}

function buildChart(labels, data) {
    if (chart) chart.destroy();
    chart = new Chart(ctx, {
        type: 'line',
        data: { labels, datasets: [{
            data,
            borderWidth: 2,
            pointRadius: data.length < 30 ? 3 : 0,
            pointHoverRadius: 4,
            fill: { target: 'origin', above: 'rgba(0,230,118,0.07)', below: 'rgba(255,82,82,0.07)' },
            tension: 0,
            stepped: true,
        }]},
        options: {
            responsive: true, maintainAspectRatio: false, animation: false,
            interaction: { intersect: false, mode: 'index' },
            scales: {
                x: { ticks: { color: '#555', maxTicksLimit: 5, font: { size: 10 } }, grid: { color: '#222' } },
                y: { ticks: { color: '#555', font: { size: 10 }, callback: v => '$'+v.toFixed(2) }, grid: { color: '#222' } }
            },
            plugins: {
                legend: { display: false },
                tooltip: { backgroundColor: '#222', titleColor: '#aaa', bodyColor: '#fff', borderColor: '#333', borderWidth: 1,
                           callbacks: { label: c => ' $'+c.parsed.y.toFixed(4) } }
            }
        },
        plugins: [{ id: 'dynColor', afterLayout: c => {
            const { ctx: cx, chartArea, scales } = c;
            if (!chartArea) return;
            const g = getGradient(cx, chartArea, scales);
            c.data.datasets[0].borderColor = g;
            c.data.datasets[0].pointBackgroundColor = g;
        }}]
    });
}

function filterChart(hours, btn) {
    document.querySelectorAll('.btn-t').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    if (hours === 0 || allTimestamps.length === 0) { buildChart(allLabels, allData); return; }
    const cutoff = Date.now() - hours * 3600000;
    const idx = allTimestamps.map((ts,i) => new Date(ts).getTime() >= cutoff ? i : -1).filter(i => i >= 0);
    buildChart(idx.map(i => allLabels[i]), idx.map(i => allData[i]));
}

buildChart(allLabels, allData);
</script>
</body>
</html>
"""


def safe_float(val, default=0.0):
    try:    return float(val)
    except: return default

def safe_int(val, default=0):
    try:    return int(val)
    except: return default


def get_data():
    files   = glob.glob(f"{CSV_FILE}*")
    df_list = [pd.read_csv(p) for p in files if os.path.exists(p)]
    if not df_list:
        return None

    try:
        df = pd.concat(df_list, ignore_index=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.sort_values("timestamp").reset_index(drop=True)
        last    = df.iloc[-1]
        central = pytz.timezone("US/Central")

        # ── PnL chart: cumulative sum of pnl_this_trade on settled rows only ──
        settled = df[df["event"].isin(["PAYOUT", "SETTLE"])].copy()
        if "pnl_this_trade" in settled.columns and not settled.empty:
            settled["cum_pnl"] = settled["pnl_this_trade"].apply(safe_float).cumsum()
        else:
            settled["cum_pnl"] = settled["bankroll"].apply(safe_float) - START_BALANCE if "bankroll" in settled.columns else 0

        chart_labels     = settled["timestamp"].dt.tz_convert(central).dt.strftime("%H:%M").tolist()
        chart_data       = settled["cum_pnl"].round(4).tolist()
        chart_timestamps = settled["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ").tolist()

        # Anchor at zero
        if chart_data:
            chart_labels     = ["start"] + chart_labels
            chart_data       = [0.0]     + chart_data
            chart_timestamps = [chart_timestamps[0]] + chart_timestamps

        realized_balance = START_BALANCE + (chart_data[-1] if len(chart_data) > 1 else 0.0)
        pnl              = realized_balance - START_BALANCE

        wins   = len(df[df["event"] == "PAYOUT"])
        losses = len(df[df["event"] == "SETTLE"])
        total  = wins + losses

        buy_rows       = df[df["event"].isin(["PAPER_BUY", "LIVE_BUY"])]
        avg_entry      = round(buy_rows["entry_price"].apply(safe_float).mean(), 1)      if len(buy_rows) else 0
        avg_spread     = round(buy_rows["spread"].apply(safe_float).mean(), 1)           if len(buy_rows) and "spread" in buy_rows.columns else 0
        avg_signal_age = round(buy_rows["signal_age_min"].apply(safe_float).mean(), 1)   if len(buy_rows) and "signal_age_min" in buy_rows.columns else 0
        pnl_series     = settled["pnl_this_trade"].apply(safe_float) if "pnl_this_trade" in settled.columns and not settled.empty else pd.Series(dtype=float)
        avg_pnl        = pnl_series.mean() if len(pnl_series) else 0.0

        now_utc   = pd.Timestamp.now("UTC")
        is_active = (now_utc - last["timestamp"]).total_seconds() < 120
        ob_stale  = bool(df.tail(5)["ob_stale"].astype(int).sum() >= 3) if "ob_stale" in df.columns else False
        mode_str  = str(last.get("mode", "PAPER"))

        birth_ts   = safe_float(last.get("signal_birth_time", 0))
        signal_age = round((time.time() - birth_ts) / 60.0, 1) if birth_ts > 0 else 999.0

        lm       = df[df["ticker"].notna() & (df["ticker"] != "")].iloc[-1] if not df.empty else last
        yes_bid  = safe_int(lm.get("raw_yes_bid", 0))
        no_bid   = safe_int(lm.get("raw_no_bid",  0))
        yes_ask  = safe_int(lm.get("ask_yes", 100 - no_bid  if no_bid  else 99))
        no_ask   = safe_int(lm.get("ask_no",  100 - yes_bid if yes_bid else 99))
        yes_liq  = safe_int(lm.get("yes_liq", 0))
        no_liq   = safe_int(lm.get("no_liq",  0))

        log_df = df[~df["event"].isin(["HRTBT", "SKIP"])].tail(20).iloc[::-1]
        logs   = []
        for _, r in log_df.iterrows():
            ev = str(r.get("event", ""))
            logs.append({
                "time":  r["timestamp"].astimezone(central).strftime("%H:%M:%S"),
                "event": ev.replace("PAPER_", "").replace("LIVE_", ""),
                "color": _event_color(ev),
                "msg":   str(r.get("msg", "")),
            })

        last_ct   = last["timestamp"].astimezone(central)

        return dict(
            last_update=last_ct.strftime("%H:%M:%S"),
            is_active=is_active, ob_stale=ob_stale, mode=mode_str,
            balance=realized_balance, pnl=pnl,
            ut_signal=last.get("ut_signal") or None,
            ut_stop=safe_float(last.get("ut_stop", 0)),
            ut_atr=safe_float(last.get("ut_atr",  0)),
            signal_age=signal_age, stalk_limit=10,
            wins=wins, losses=losses, total=total,
            win_rate=(wins / total * 100) if total > 0 else 0.0,
            avg_entry=avg_entry, avg_spread=avg_spread,
            avg_signal_age=avg_signal_age, avg_pnl=avg_pnl,
            ticker=str(lm.get("ticker", "--")),
            time_left=safe_float(lm.get("time_left", 0)),
            strike=safe_float(lm.get("strike", 0)),
            obi=safe_float(lm.get("obi", 0)),
            yes_bid=yes_bid, no_bid=no_bid,
            yes_ask=yes_ask, no_ask=no_ask,
            yes_liq=yes_liq, no_liq=no_liq,
            chart_labels=chart_labels,
            chart_data=chart_data,
            chart_timestamps=chart_timestamps,
            logs=logs,
        )

    except Exception as e:
        print(f"Dashboard error: {e}")
        import traceback; traceback.print_exc()
        return None


@app.route("/")
def home():
    data = get_data()
    return render_template_string(HTML_TEMPLATE, **data) if data else "Waiting for bot data..."

@app.route("/health")
def health():
    data = get_data()
    if not data:
        return {"status": "starting"}, 503
    return {"status": "ok", "last_update": data["last_update"]}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

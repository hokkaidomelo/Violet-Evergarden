"""
Evergreen Engine - weekly objective data scan
--------------------------------------------
Pulls only the OBJECTIVE, numeric parts of the framework (macro, FX, price/valuation,
technical) from free data sources. Subjective parts (moat quality, catalysts, NRR from
filings) are still meant to be judged by you + Claude afterward - this script does not
replace the first-layer moat review.

Free data sources used (no paid API keys, no LLM tokens spent):
  - FRED (10Y treasury yield)        -> needs a free API key, register at https://fred.stlouisfed.org/docs/api/api_key.html
  - open.er-api.com (USD/TWD FX)     -> free, no key
  - yfinance (price, 52wk range, PE, EV/EBITDA, P/B, 200MA)  -> free, no key

Usage:
  pip install yfinance requests
  set FRED_API_KEY=xxxxxxxx        (Windows PowerShell: $env:FRED_API_KEY="xxxx")
  python weekly_scan.py
"""

import os
import json
import datetime
import requests
import yfinance as yf

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")

# Edit this list freely - one ticker per candidate you want scanned every week.
CANDIDATES = {
    "MU": "B",      # hardware/semiconductor track
    "AVGO": "B",
    "NVDA": "B",
    "GOOGL": "C",   # platform/ecosystem track
    "META": "C",
    "AMZN": "C",
    "NOW": "A",     # SaaS track
    "CRM": "A",
}

REPORT_DIR = os.path.join(os.path.dirname(__file__), "reports")
DOCS_DIR = os.path.join(os.path.dirname(__file__), "docs")


def get_10y_yield():
    if not FRED_API_KEY:
        return None, "FRED_API_KEY not set - skipped"
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": "DGS10",
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 1,
    }
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    obs = r.json()["observations"][0]
    return float(obs["value"]), obs["date"]


def get_usd_twd():
    url = "https://open.er-api.com/v6/latest/USD"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()
    return data["rates"]["TWD"], data["time_last_update_utc"]


def fx_score(usd_twd):
    if usd_twd is None:
        return None, None
    if usd_twd < 30:
        return 4, 3  # (us_stock_score, tw_stock_score)
    if usd_twd > 33.5:
        return 0, 4
    return 3, 3


def macro_mode(ten_y):
    if ten_y is None:
        return "unknown (10Y not fetched)"
    return "tightening" if ten_y > 4.5 else "neutral/easing"


def scan_ticker(ticker, track):
    t = yf.Ticker(ticker)
    info = t.info or {}
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    high52 = info.get("fiftyTwoWeekHigh")
    low52 = info.get("fiftyTwoWeekLow")
    ma200 = info.get("twoHundredDayAverage")

    pullback_from_high = None
    if price and high52:
        pullback_from_high = round((1 - price / high52) * 100, 1)

    above_200ma = None
    if price and ma200:
        above_200ma = price > ma200

    valuation = {}
    if track == "B":
        valuation["ev_to_ebitda"] = info.get("enterpriseToEbitda")
        valuation["price_to_book"] = info.get("priceToBook")
        valuation["ev_to_revenue"] = info.get("enterpriseToRevenue")
    elif track == "C":
        valuation["price_to_sales"] = info.get("priceToSalesTrailing12Months")
        valuation["ev_to_ebitda"] = info.get("enterpriseToEbitda")
    elif track == "A":
        valuation["ev_to_revenue"] = info.get("enterpriseToRevenue")
        valuation["free_cashflow"] = info.get("freeCashflow")

    return {
        "ticker": ticker,
        "track": track,
        "price": price,
        "52w_high": high52,
        "52w_low": low52,
        "pullback_from_high_pct": pullback_from_high,
        "200ma": ma200,
        "above_200ma": above_200ma,
        "valuation": valuation,
        "eps_growth_note": "check info['earningsGrowth'] manually if needed",
        "earnings_growth": info.get("earningsGrowth"),
    }


def render_html(report):
    rows = []
    for c in report["candidates"]:
        if "error" in c:
            rows.append(f"""
            <div class="tbl-row"><div class="tr-head">{c['ticker']}</div>
            <div class="tr-body"><div class="kv"><span class="k">error</span><span class="v">{c['error']}</span></div></div></div>""")
            continue
        val_items = "".join(
            f'<div class="kv"><span class="k">{k}</span><span class="v">{v}</span></div>'
            for k, v in c["valuation"].items() if v is not None
        )
        pullback = c["pullback_from_high_pct"]
        pullback_str = f"{pullback}%" if pullback is not None else "n/a"
        ma_str = "above" if c["above_200ma"] else ("below" if c["above_200ma"] is not None else "n/a")
        rows.append(f"""
        <div class="tbl-row">
          <div class="tr-head">{c['ticker']} · track {c['track']}</div>
          <div class="tr-body">
            <div class="kv"><span class="k">price</span><span class="v">{c['price']}</span></div>
            <div class="kv"><span class="k">52w high / low</span><span class="v">{c['52w_high']} / {c['52w_low']}</span></div>
            <div class="kv"><span class="k">pullback from high</span><span class="v">{pullback_str}</span></div>
            <div class="kv"><span class="k">200MA</span><span class="v">{ma_str} ({c['200ma']})</span></div>
            <div class="kv"><span class="k">earnings growth</span><span class="v">{c['earnings_growth']}</span></div>
            {val_items}
          </div>
        </div>""")

    html = f"""<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Evergreen Engine - Weekly Scan</title>
<style>
  :root{{ --bg:#0b0d12; --panel:#12151d; --panel2:#171b26; --ink:#e8e6df; --ink-dim:#9a9a94; --gold:#c9974a; --line:#262b38; }}
  body{{ margin:0; background:var(--bg); color:var(--ink); font-family:-apple-system,"PingFang TC","Noto Sans TC",sans-serif; padding:20px; }}
  h1{{ font-size:20px; }} .sub{{ color:var(--ink-dim); font-size:13px; margin-bottom:20px; }}
  .tbl-row{{ border:1px solid var(--line); border-radius:10px; margin-bottom:10px; overflow:hidden; max-width:600px; }}
  .tr-head{{ background:var(--panel2); padding:8px 12px; font-family:monospace; color:var(--gold); font-size:12px; }}
  .tr-body{{ padding:10px 12px; font-size:13.5px; background:var(--panel); }}
  .kv{{ display:flex; justify-content:space-between; gap:10px; padding:3px 0; border-bottom:1px dashed var(--line); }}
  .kv:last-child{{ border-bottom:none; }}
  .k{{ color:var(--ink-dim); }} .v{{ text-align:right; font-weight:500; }}
</style></head>
<body>
  <h1>Evergreen Engine · Weekly Objective Scan</h1>
  <div class="sub">run: {report['run_date']} &nbsp;|&nbsp; 10Y yield: {report['macro']['10y_yield']} ({report['macro']['mode']}) &nbsp;|&nbsp; USD/TWD: {report['fx']['usd_twd']}</div>
  {"".join(rows)}
  <p class="sub">Objective data only (price/valuation/macro/FX). Moat, catalyst and NRR judgment still need manual review with Claude.</p>
</body></html>"""
    return html


def main():
    os.makedirs(REPORT_DIR, exist_ok=True)
    os.makedirs(DOCS_DIR, exist_ok=True)
    today = datetime.date.today().isoformat()

    ten_y, ten_y_date = get_10y_yield()
    usd_twd, fx_date = get_usd_twd()
    us_fx, tw_fx = fx_score(usd_twd)

    report = {
        "run_date": today,
        "macro": {
            "10y_yield": ten_y,
            "10y_yield_asof": ten_y_date,
            "mode": macro_mode(ten_y),
        },
        "fx": {
            "usd_twd": usd_twd,
            "asof": fx_date,
            "us_stock_fx_score": us_fx,
            "tw_stock_fx_score": tw_fx,
        },
        "candidates": [],
    }

    for ticker, track in CANDIDATES.items():
        try:
            report["candidates"].append(scan_ticker(ticker, track))
        except Exception as e:
            report["candidates"].append({"ticker": ticker, "error": str(e)})

    out_path = os.path.join(REPORT_DIR, f"scan_{today}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    latest_path = os.path.join(REPORT_DIR, "latest.json")
    with open(latest_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    html_path = os.path.join(DOCS_DIR, "index.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_html(report))

    print(f"Saved report -> {out_path}")
    print(f"Saved website -> {html_path}")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

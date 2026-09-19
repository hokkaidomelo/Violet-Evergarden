"""
Evergreen Engine - weekly stock scan
-------------------------------------
Free part: macro (FRED), FX (open.er-api.com), price/valuation/technical (yfinance).
Paid part (small, optional): momentum/catalyst/insider-selling judgment via a Claude
Haiku API call with web search, since that requires reading and reasoning over news,
not just pulling a number. Skips gracefully (falls back to the 45pt objective-only
score) if ANTHROPIC_API_KEY is not set - the free path always still works.

First-layer moat review (NRR, Network Effect, Switching Cost) is still NOT automated -
that needs reading actual filings/qualitative judgment and stays a manual step with
Claude in chat.

Usage:
  pip install -r requirements.txt
  set FRED_API_KEY=xxxxxxxx            (Windows PowerShell: $env:FRED_API_KEY="xxxx")
  set ANTHROPIC_API_KEY=xxxxxxxx       (optional - omit to stay 100% free)
  python weekly_scan.py
"""

import os
import json
import re
import datetime
import requests
import yfinance as yf

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
LLM_MODEL = "claude-haiku-4-5-20251001"  # cheapest current model - keeps weekly cost tiny

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


def valuation_score(pullback_pct, mode):
    """Proxy for the 32-point '相對估值位置' dimension using pullback-from-high as
    a stand-in for a true historical PR percentile (Rule D: yfinance has no clean
    percentile field for free, so this is an ESTIMATE, flagged as such in the UI)."""
    if pullback_pct is None:
        return None
    if mode == "tightening":
        bands = [(30, 32), (10, 22), (0.01, 11), (-999, 0)]
    else:
        bands = [(20, 32), (5, 22), (0.01, 11), (-999, 0)]
    for threshold, score in bands:
        if pullback_pct >= threshold:
            return score
    return 0


def technical_score(above_200ma):
    if above_200ma is None:
        return None
    return 10 if above_200ma else 0


def partial_score(c, mode, us_fx):
    v = valuation_score(c.get("pullback_from_high_pct"), mode)
    t = technical_score(c.get("above_200ma"))
    parts = [x for x in (v, t) if x is not None]
    if not parts:
        return None
    total = sum(parts) + (us_fx or 0)
    return {
        "valuation_pts": v,
        "technical_pts": t,
        "fx_pts": us_fx,
        "objective_total": total,
        "objective_max": 32 + 10 + 3,
    }


def llm_subjective_score(ticker, track):
    """Uses Claude (with the web_search tool) to judge the 55 points the objective
    data sources cannot answer: momentum (25), catalyst (20), insider selling (10).
    Costs a small amount of real money (Haiku model + a few web searches) - this is
    the one part of the framework that is NOT free. Returns None on any failure so
    the rest of the report still works without it."""
    if not ANTHROPIC_API_KEY:
        return None
    try:
        import anthropic
    except ImportError:
        return None

    prompt = f"""You are scoring {ticker} (track {track}, A=SaaS/B=hardware-semis/C=platform)
for a monthly tech-stock screening framework. Search for this week's news and answer
ONLY with a JSON object, no other text:

{{
  "momentum_pts": <int 0-25>,   // 25=beat estimates AND raised guidance, 15=in-line, 0=cut guidance
  "catalyst_pts": <int 0-20>,   // 20=clear positive company/industry catalyst this week, 10=none notable, 0=negative catalyst
  "insider_pts": <int 0 or 10>, // 0=large NON-prearranged insider selling (Form 4, not 10b5-1/Form 144 plan), else 10
  "notes": "<one short sentence citing what you found>"
}}"""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=LLM_MODEL,
        max_tokens=600,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
        messages=[{"role": "user", "content": prompt}],
    )

    text = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None

    momentum = max(0, min(25, int(data.get("momentum_pts", 0))))
    catalyst = max(0, min(20, int(data.get("catalyst_pts", 0))))
    insider = 10 if int(data.get("insider_pts", 10)) >= 5 else 0
    return {
        "momentum_pts": momentum,
        "catalyst_pts": catalyst,
        "insider_pts": insider,
        "subjective_total": momentum + catalyst + insider,
        "subjective_max": 55,
        "notes": data.get("notes", ""),
    }


def scan_ticker(ticker, track, mode, us_fx):
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

    c = {
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
    c["score"] = partial_score(c, mode, us_fx)

    subj = llm_subjective_score(ticker, track)
    c["subjective_score"] = subj
    if subj is not None and c["score"] is not None:
        c["full_score"] = {
            "total": c["score"]["objective_total"] + subj["subjective_total"],
            "max": c["score"]["objective_max"] + subj["subjective_max"],  # 100
        }
    else:
        c["full_score"] = None
    return c


def rank_key(c):
    """Sort by the full 100-point score when the LLM subjective step succeeded,
    otherwise fall back to the 45-point objective-only score."""
    if c.get("full_score") is not None:
        return c["full_score"]["total"]
    return c["score"]["objective_total"]


def format_score(c):
    if c.get("full_score") is not None:
        s, sub = c["score"], c["subjective_score"]
        return (f'{c["full_score"]["total"]}/100 (估值{s["valuation_pts"]}+動能{sub["momentum_pts"]}'
                f'+催化劑{sub["catalyst_pts"]}+技術{s["technical_pts"]}+內部人{sub["insider_pts"]}+FX{s["fx_pts"]})')
    s = c["score"]
    return f'{s["objective_total"]}/45 客觀分數（估值{s["valuation_pts"]}+技術{s["technical_pts"]}+FX{s["fx_pts"]}，動能/催化劑/內部人未取得）'


def render_overall_top3(report):
    valid = [c for c in report["candidates"] if "error" not in c and c.get("score") is not None]
    top = sorted(valid, key=rank_key, reverse=True)[:3]
    if not top:
        return '<div class="tbl-row"><div class="tr-head">本週綜合前3名</div><div class="tr-body">本週無資料</div></div>'
    rows = "".join(
        f'<div class="kv"><span class="k">#{i+1} {c["ticker"]} (軌{c["track"]})</span>'
        f'<span class="v">{format_score(c)}</span></div>'
        for i, c in enumerate(top)
    )
    return f'<div class="tbl-row"><div class="tr-head">本週綜合前3名</div><div class="tr-body">{rows}</div></div>'


def render_top3(report):
    by_track = {"A": [], "B": [], "C": []}
    for c in report["candidates"]:
        if "error" in c or c.get("score") is None:
            continue
        by_track.setdefault(c["track"], []).append(c)

    track_names = {"A": "A軌 · SaaS", "B": "B軌 · 硬體/半導體", "C": "C軌 · 生態系平台"}
    blocks = []
    for track in ("A", "B", "C"):
        items = sorted(by_track.get(track, []), key=rank_key, reverse=True)[:3]
        if not items:
            blocks.append(f'<div class="tbl-row"><div class="tr-head">{track_names[track]}</div><div class="tr-body">本週無資料</div></div>')
            continue
        rows = "".join(
            f'<div class="kv"><span class="k">#{i+1} {c["ticker"]}</span><span class="v">{format_score(c)}</span></div>'
            for i, c in enumerate(items)
        )
        blocks.append(f'<div class="tbl-row"><div class="tr-head">{track_names[track]}</div><div class="tr-body">{rows}</div></div>')
    return "".join(blocks)


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
  <h1>Evergreen Engine · Weekly Scan</h1>
  <div class="sub">run: {report['run_date']} &nbsp;|&nbsp; 10Y yield: {report['macro']['10y_yield']} ({report['macro']['mode']}) &nbsp;|&nbsp; USD/TWD: {report['fx']['usd_twd']}</div>

  <h2 style="font-size:15px;">本週綜合前3名（滿分100：估值32+動能25+催化劑20+技術10+內部人10+FX3）</h2>
  {render_overall_top3(report)}

  <h2 style="font-size:15px; margin-top:24px;">各軌前3名</h2>
  {render_top3(report)}

  <h2 style="font-size:15px; margin-top:24px;">完整原始資料</h2>
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
            report["candidates"].append(scan_ticker(ticker, track, report["macro"]["mode"], us_fx))
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

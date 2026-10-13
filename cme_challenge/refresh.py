"""One command before trading: refresh every input and rebuild the dashboard.

Steps (each one that cannot run on this machine is skipped, and the dashboard says so):
  1. --databento (any PC with the API key) or --bloomberg (PC with a logged-in Terminal): update contract
     histories and the roll-correct series
  2. retrain the model when the series are newer than it
  3. latest prices and signals for every market from Yahoo (cme_challenge/live.py)
  4. US heating-demand outlook from three weather models (cme_challenge/weather.py)
  5. write <data>/futures/dashboard.html; with --watch it is rebuilt and reloads itself in the browser

Usage (from the repository root):
    python cme_challenge/refresh.py                  # once, then open the dashboard
    python cme_challenge/refresh.py --watch 15       # every 15 minutes until Ctrl+C
    python cme_challenge/refresh.py --databento      # also add the latest days of Databento contract history
    python cme_challenge/refresh.py --bloomberg      # on a Bloomberg PC: also update contract histories
"""
import argparse
import html
import subprocess
import sys
import time
import traceback
import webbrowser
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repository root
from data_root import DATA

import live
import weather

REPO = Path(__file__).resolve().parents[1]
FUT = DATA / "futures"
DASHBOARD = FUT / "dashboard.html"
CHALLENGE_END = pd.Timestamp("2026-10-30 16:00", tz="America/Chicago")
CT = "America/Chicago"
MAX_IDEAS = 8


def run_script(path, *args):
    out = subprocess.run([sys.executable, str(REPO / path), *args], capture_output=True, text=True)
    return out.returncode, (out.stdout + out.stderr).strip().splitlines()[-1:] or [""]


def update_history(notes, databento, bloomberg):
    if databento:
        code, last = run_script("cme_challenge/databento_contracts.py")
        notes.append(f"Databento contracts: {'updated' if code == 0 else f'stopped (exit {code}): {last[0]}'}")
    if bloomberg:
        code, last = run_script("bloomberg/futures_contracts.py")
        notes.append(f"Bloomberg contracts: {'updated' if code == 0 else f'stopped (exit {code}): {last[0]}'}")
    if (FUT / "contracts.parquet").exists() or (FUT / "databento" / "contracts.parquet").exists():
        code, last = run_script("cme_challenge/continuous.py")
        notes.append(f"Roll-correct series: {'rebuilt' if code == 0 else last[0]}")


def update_model(notes):
    series = list((FUT / "continuous").glob("*.parquet"))
    model = FUT / "model" / "model.json"
    if not series:
        notes.append("Model: needs contract history (cme_challenge/databento_contracts.py, or Bloomberg)")
        return
    if model.exists() and model.stat().st_mtime >= max(p.stat().st_mtime for p in series):
        return
    code, last = run_script("cme_challenge/model.py")
    notes.append(f"Model: {'retrained' if code == 0 else 'retraining failed: ' + last[0]}")


# --------------------------------------------------------------------------- dashboard

def arrow(value):
    if value is None or not np.isfinite(value) or value == 0:
        return '<span class="flat">·</span>'
    return '<span class="up">▲</span>' if value > 0 else '<span class="down">▼</span>'


def text(value):
    """Plain cell text; pandas gives NaN for missing strings."""
    return html.escape(value) if isinstance(value, str) else ""


def num(value, digits=2, signed=False):
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return "—"
    return f"{value:+,.{digits}f}" if signed else f"{value:,.{digits}f}"


def signed_cell(value, digits=2):
    if value is None or not np.isfinite(value):
        return '<td class="num">—</td>'
    if round(value, digits) == 0:
        return f'<td class="num">{0:.{digits}f}</td>'
    cls = "up" if value > 0 else "down" if value < 0 else ""
    return f'<td class="num {cls}">{value:+,.{digits}f}</td>'


def ideas_rows(table):
    agree = table[(table["votes"].abs() >= 2) & (np.sign(table["trend"]) == np.sign(table["votes"]))].copy()
    agree["strength"] = agree["votes"].abs() + agree["model_view"].abs().fillna(0) + agree["trend"].abs()
    rows = []
    for _, r in agree.sort_values("strength", ascending=False).head(MAX_IDEAS).iterrows():
        side = "LONG" if r["votes"] > 0 else "SHORT"
        x = r["size_x"]
        size = f"{num(r['contracts_for_risk'] * x, 1)} contracts ({x:g}x)"
        if text(r["micro"]) and np.isfinite(r["micros_for_risk"]):
            size += f" or {num(r['micros_for_risk'] * x, 0)} {text(r['micro'])}"
        rows.append(f"<tr><td><b>{html.escape(r['name'])}</b></td><td>{html.escape(r['contract'])}</td>"
                    f'<td class="{"up" if side == "LONG" else "down"}"><b>{side}</b></td>'
                    f"<td>{arrow(r['trend'])} trend {arrow(r['carry_pct_yr'])} carry {arrow(r['seasonal'])} season "
                    f"{arrow(r['model_view'])} model</td><td class=num>{num(r['last'], 4 if r['last'] < 10 else 2)}</td>"
                    f"<td>{size}</td></tr>")
    return "".join(rows) or '<tr><td colspan="6">No market where the signals clearly agree right now.</td></tr>'


def market_rows(table):
    rows = []
    for sector, group in table.groupby("sector", sort=False):
        rows.append(f'<tr class="sector"><td colspan="15">{html.escape(sector)}</td></tr>')
        for _, r in group.iterrows():
            trends = "".join(arrow(r[c]) for c in ["trend_21d", "trend_63d", "trend_126d", "trend_252d"])
            micro = f"{num(r['micros_for_risk'] * r['size_x'], 0)} {text(r['micro'])}" if text(r["micro"]) else ""
            note = text(r.get("note"))
            rows.append(
                f"<tr><td><b>{html.escape(r['root'])}</b> {html.escape(r['name'])}</td><td>{html.escape(r['contract'])}</td>"
                f"<td class=num>{num(r['last'], 4 if r['last'] < 10 else 2)}</td>{signed_cell(r['change_pct'])}"
                f"<td class=trend>{trends}</td>{signed_cell(r['carry_pct_yr'], 1)}{signed_cell(r['seasonal'])}"
                f"{signed_cell(r['model_view'])}{signed_cell(float(r['votes']), 0)}<td class=num>{num(r['vol_pct_yr'], 0)}</td>"
                f"<td class=num>{num(r['daily_vol_usd'], 0)}</td><td class=num>{num(r['contracts_for_risk'] * r['size_x'], 1)} <span class=muted>({r['size_x']:g}x)</span></td>"
                f"<td class=num>{micro}</td><td>{text(r['expiry'])}</td><td class=note>{note}</td></tr>")
    return "".join(rows)


def weather_block(outlook, errors):
    if outlook is None:
        return f"<p class=muted>Weather unavailable: {html.escape('; '.join(errors))}</p>"
    if "tdd_days_1_7" not in outlook:  # saved before cooling demand was added
        return "<p class=muted>Weather outlook from an older version; the next refresh replaces it.</p>"
    rows = []
    for _, r in outlook.iterrows():
        change = r.get("change_vs_previous", np.nan)
        rows.append(f"<tr><td>{html.escape(r['model'])}</td><td class=num>{num(r['hdd_days_1_7'], 0)}</td>"
                    f"<td class=num>{num(r['cdd_days_1_7'], 0)}</td>{signed_cell(r['vs_normal_days_1_7'], 0)}"
                    f"<td class=num>{num(r['tdd_days_8_15'], 0)}</td>{signed_cell(r['vs_normal_days_8_15'], 0)}"
                    f"{signed_cell(change, 1)}</tr>")
    extra = "".join(f"<p class=muted>Could not fetch {html.escape(e)}</p>" for e in errors)
    return ("<table><thead><tr><th>Model</th><th>HDD days 1–7</th><th>CDD days 1–7</th><th>total vs normal</th>"
            "<th>total days 8–15</th><th>vs normal</th><th>change since last run</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>"
            "<p class=muted>Gas-weighted degree days in 26 US cities: heating (cities weighted by homes heated with "
            "gas) plus cooling (weighted by gas in their power generation). Positive = more gas demand than normal "
            "or than the previous run, supportive for natural gas. The tested record is in the signal database; "
            "when the models disagree, the outlook is uncertain.</p>" + extra)


def model_block(model):
    if not model:
        return ("<p class=muted>Not trained yet: it needs contract history (cme_challenge/databento_contracts.py "
                "or bloomberg/futures_contracts.py, then cme_challenge/continuous.py and model.py).</p>")
    oos = model["out_of_sample"]
    rows = "".join(f"<tr><td>{html.escape(k)}</td><td class=num>{num(100 * v['hit_rate'], 0)}%</td>"
                   f"<td class=num>{num(v['sharpe'])}</td><td class=num>{num(v['sharpe_last_5y'])}</td></tr>"
                   for k, v in oos.items())
    return (f"<p class=muted>Ridge regression on trend, carry and seasonality across {model['markets']} markets, "
            f"trained through {model['trained_through']}. Model view = expected move over the next "
            f"{model['horizon']} trading days in units of a normal move (+0.3 ≈ a third of a typical move up). "
            "Out-of-sample record, before costs:</p><table><thead><tr><th>Signal</th><th>Hit rate</th>"
            "<th>Sharpe</th><th>Sharpe, last 5 years</th></tr></thead><tbody>" + rows + "</tbody></table>")


def next_fed_decision(now):
    """Reminder line for the next scheduled FOMC decision (dates from event_strategies.py)."""
    import event_strategies as ev
    dates = [ev.last_day(y, t) for y, meetings in ev.RECENT_MEETINGS.items() for t in meetings]
    upcoming = [d for d in dates if d.date() >= now.date()]
    if not upcoming:
        return ""
    d = min(upcoming)
    return (f" Next Fed decision: <b>{d:%a %d %b}</b>, 1 pm CT (since 2000 the dollar has tended to weaken and bonds "
            "to firm on the day; the equity drift has faded; see event_strategies.py).")


def render(table, model, outlook, weather_errors, notes, watch, risk):
    now = pd.Timestamp.now(tz=CT)
    days_left = int(np.busday_count(now.date(), (CHALLENGE_END + pd.Timedelta(days=1)).date()))
    fed = next_fed_decision(now)
    refresh_tag = f'<meta http-equiv="refresh" content="{int(watch * 60)}">' if watch else ""
    freshest = max(table["as_of"]) if len(table) else None
    notes_html = "".join(f"<li>{html.escape(n)}</li>" for n in notes)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{refresh_tag}
<title>CME Challenge Desk</title>
<style>
:root {{ --bg:#f7f8fa; --card:#fff; --ink:#1f2933; --muted:#5b6770; --rule:#e2e6ea; --up:#15803d; --down:#c2410c;
        --accent:#1d4ed8; --warn-bg:#fff5ec; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#111418; --card:#1a1f25; --ink:#e6e9ec; --muted:#9aa5ae;
        --rule:#2c333b; --up:#4ade80; --down:#fb923c; --accent:#93b4ff; --warn-bg:#2a2016; }} }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; }}
a {{ color:var(--accent); }}
main {{ max-width:1400px; margin:0 auto; padding:16px; }}
h1 {{ font-size:22px; margin:4px 0; }} h2 {{ font-size:16px; margin:0 0 8px; }}
.card {{ background:var(--card); border:1px solid var(--rule); border-radius:8px; padding:14px; margin:12px 0; overflow-x:auto; }}
.bar {{ background:var(--warn-bg); border-radius:8px; padding:10px 14px; margin:10px 0; }}
table {{ border-collapse:collapse; width:100%; }} th, td {{ padding:5px 8px; border-bottom:1px solid var(--rule);
        text-align:left; white-space:nowrap; }}
th {{ font-weight:600; color:var(--muted); font-size:12px; }} td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
tr.sector td {{ font-weight:600; color:var(--accent); padding-top:12px; }}
.up {{ color:var(--up); }} .down {{ color:var(--down); }} .flat, .muted {{ color:var(--muted); }}
.muted {{ font-size:12px; }} td.note {{ color:var(--muted); font-size:12px; white-space:normal; min-width:180px; }} td.trend span {{ margin-right:2px; }}
</style></head><body><main>
<h1>CME Challenge Desk</h1>
<div class="muted">Updated {now:%a %d %b %H:%M} CT · prices from Yahoo (delayed about 10 min; CQG shows the live price),
latest bar {freshest} · sizes for ${risk:,.0f} of daily volatility per position{f' · reloads every {watch:g} min' if watch else ''}
· <a href="cme/cme.html">signal database</a> (run <code>cme</code> to update)</div>
<div class="bar"><b>{days_left} trading days left.</b> Be flat before <b>Fri 30 Oct, 16:00 CT</b> (open contracts are
penalised) and trade at least <b>10 contracts every day</b>.{fed}</div>
<div class="card"><h2>Where the signals agree</h2><table><thead><tr><th>Market</th><th>Contract</th><th>Side</th>
<th>Signals</th><th>Last</th><th>Size</th></tr></thead><tbody>{ideas_rows(table)}</tbody></table>
<p class=muted>The strongest {MAX_IDEAS} markets where at least two of trend, carry, seasonality and the model point the same
way, trend included; ranked by votes, model view and how consistent the trend is. A starting point for discussion, not an
order: check the chart, the news and upcoming reports before trading.</p></div>
<div class="card"><h2>Natural gas weather</h2>{weather_block(outlook, weather_errors)}</div>
<div class="card"><h2>All markets</h2><table><thead><tr><th>Market</th><th>Contract</th><th>Last</th><th>Day %</th>
<th>Trend 1·3·6·12m</th><th>Carry %/yr</th><th>Season</th><th>Model</th><th>Votes</th><th>Vol %/yr</th>
<th>$ vol/day/contract</th><th>Contracts</th><th>Micros</th><th>Expiry</th><th></th></tr></thead>
<tbody>{market_rows(table)}</tbody></table>
<p class=muted>Sizes target equal risk: contracts carrying the chosen daily volatility, from the last 21 days'
volatility and never more than twice the size the 1-year volatility gives, times a conviction multiplier: 0.5x when
signals are mixed, 1x when trend and the model agree, 2x when all four trend lookbacks and the model agree. Keep any
one market under about a quarter of the account's risk, and cut exposure after a 20-25% loss. Adjust an open position only when its
suggested size moves by more than 25% (same results, half the trades). Trend: direction of the traded contract's own price over 1, 3, 6 and 12 months. Carry: annualised gap to the
next contract (positive = backwardation, holding earns it). Season: average move over the next 15 trading days in past
years, from the roll-correct Bloomberg series (units of a normal move). Votes: sum of the signs of trend, carry, season
and model.</p></div>
<div class="card"><h2>Model</h2>{model_block(model)}</div>
<div class="card"><h2>Pipeline</h2><ul class=muted>{notes_html or '<li>All steps ran.</li>'}</ul></div>
</main></body></html>"""


def refresh_once(args, first):
    notes = []
    if (args.databento or args.bloomberg) and first:
        update_history(notes, args.databento, args.bloomberg)
    update_model(notes)
    table, model = live.refresh(args.risk)
    try:
        outlook, weather_errors = weather.refresh()
    except Exception as e:  # weather is context; never block the prices: show the last good outlook
        outlook = weather.last_outlook()
        weather_errors = [f"latest fetch failed ({e})" + (f"; showing the outlook fetched at "
                                                           f"{outlook['fetched_at'].iloc[0]}" if outlook is not None
                                                           and "fetched_at" in outlook else "")]
    FUT.mkdir(parents=True, exist_ok=True)
    DASHBOARD.write_text(render(table, model, outlook, weather_errors, notes, args.watch, args.risk), encoding="utf-8")
    print(f"{pd.Timestamp.now():%H:%M} dashboard updated: {DASHBOARD}" + "".join(f"\n  {n}" for n in notes))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--watch", type=float, help="minutes between refreshes (runs until Ctrl+C)")
    ap.add_argument("--databento", action="store_true", help="also add the latest Databento contract history")
    ap.add_argument("--bloomberg", action="store_true", help="also update contract histories (Bloomberg PC)")
    ap.add_argument("--risk", type=float, default=5000, help="daily volatility in USD per position, for sizing")
    ap.add_argument("--no-browser", action="store_true", help="do not open the dashboard")
    args = ap.parse_args()
    first = True
    while True:
        try:
            refresh_once(args, first)
            if first and not args.no_browser:
                webbrowser.open(DASHBOARD.as_uri())
        except KeyboardInterrupt:
            raise
        except Exception:  # keep watching through a bad fetch; the last good dashboard stays
            traceback.print_exc()
        first = False
        if not args.watch:
            break
        time.sleep(args.watch * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped.")

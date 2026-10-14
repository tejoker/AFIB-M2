"""Build the request that asks GPT to read the news for our CME markets, from headlines you paste in a text file.

GPT gets only the headlines, the time and the list of markets with today's contracts; it does not see our signals,
so its reading stays independent of them. The instructions and the answer's JSON schema are in
cme_challenge/gpt_news_request.json (temperature 0, strict schema, every judgement citing the headline it rests on).

Headlines file: one headline per line, optionally "HH:MM | source | text" (times in Chicago time, today);
blank lines and lines starting with # are skipped.

Writes two files in <data>/futures/news/:
  request_<time>.json  the API request body: POST it to https://api.openai.com/v1/chat/completions
  prompt_<time>.txt    the same as one message to paste into ChatGPT (no API key, no cost per call)

Ask twice and keep only what both answers agree on: a reading that changes between two runs is not reliable.

Usage (from the repository root):
    python cme_challenge/news_request.py headlines.txt
    python cme_challenge/news_request.py headlines.txt --markets CL NG HO RB    # only these roots
"""
import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repository root
from data_root import DATA
from futures_markets import MARKETS

TEMPLATE = Path(__file__).with_name("gpt_news_request.json")
LATEST = DATA / "futures" / "live" / "latest.csv"
OUT = DATA / "futures" / "news"
CT = "America/Chicago"


def read_headlines(path, today):
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        when, source, text = (parts + ["", ""])[:3] if len(parts) >= 3 else ("", "", line)
        if when:
            when = f"{today} {when} CT"
        rows.append({"id": f"h{len(rows) + 1}", "time": when or "unknown", "source": source or "unknown", "text": text})
    if not rows:
        raise SystemExit(f"No headlines in {path}")
    return rows


def market_list(roots):
    latest = pd.read_csv(LATEST).set_index("root") if LATEST.exists() else pd.DataFrame()
    out = []
    for key, m in MARKETS.items():
        root = key.strip()
        if roots and root not in roots and m.yahoo not in roots:
            continue
        row = {"market": m.yahoo, "name": m.name, "sector": m.sector}
        if root in latest.index:
            r = latest.loc[root]
            row.update({"contract": r["contract"], "last": round(float(r["last"]), 6),
                        "change_today_pct": round(float(r["change_pct"]), 2), "price_as_of": str(r["as_of"])})
        out.append(row)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("headlines", help="text file with one headline per line")
    ap.add_argument("--markets", nargs="+", help="only these roots (CME or Bloomberg symbols)")
    args = ap.parse_args()
    now = pd.Timestamp.now(tz=CT)
    request = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    data = {"as_of": f"{now:%Y-%m-%d %H:%M} CT",
            "markets": market_list(set(args.markets) if args.markets else None),
            "headlines": read_headlines(args.headlines, f"{now:%Y-%m-%d}")}
    request["messages"][1]["content"] = json.dumps(data, ensure_ascii=False, indent=1)
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = f"{now:%Y%m%d_%H%M}"
    body = OUT / f"request_{stamp}.json"
    body.write_text(json.dumps(request, ensure_ascii=False, indent=1), encoding="utf-8")
    schema = request["response_format"]["json_schema"]["schema"]
    prompt = OUT / f"prompt_{stamp}.txt"
    prompt.write_text(f"{request['messages'][0]['content']}\n\nAnswer with JSON only, matching this schema:\n"
                      f"{json.dumps(schema, indent=1)}\n\nInput:\n{request['messages'][1]['content']}\n",
                      encoding="utf-8")
    print(f"{len(data['headlines'])} headlines, {len(data['markets'])} markets, as of {data['as_of']}")
    print(f"API body:        {body}")
    print(f"ChatGPT message: {prompt}")


if __name__ == "__main__":
    main()

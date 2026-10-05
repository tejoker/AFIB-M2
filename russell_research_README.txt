RUSSELL RESEARCH DATA

Collect: .venv\Scripts\python.exe collect_russell_research.py
Resume: run the same command; complete API responses are reused from gzip JSON cache.
Build exports from saved data: add --build-only.
Changing date windows or batch sizes requires a different --output directory.

Default scope
- Russell 1000 (RIY Index), Russell 2000 (RTY Index), S&P 500 (SPX Index).
- Current membership requested on 2026-10-05.
- Historical quarter-end memberships, 2020-12-31 through 2026-09-30.
- The collection universe is the UNION of historical and current members, including
  delisted tickers returned by Bloomberg, not just companies that survived until today.
- Daily history from 2021-01-01 through 2026-10-02, with a 40-day return warmup.
- Quarterly fiscal financial history from 2015-01-01 through 2026-10-02.
- Monthly historical valuation measures and daily index benchmarks.
- Current fundamental/reference snapshot; separately overridden 1BF and 2BF consensus forecasts.

Default destination: data\russell_research_20261005

Useful outputs
- index_membership.parquet/.csv: observed security memberships and original member strings.
- security_reference_snapshot.parquet: all requested securities and latest identifiers/features.
- company_reference_snapshot.parquet/.csv: one company row and aggregated CURRENT index flags.
- daily_parts: actual consolidated traded USD, share volume, market cap, historical float,
  total return index and separately split-adjusted prices. Files are per batch.
- fundamental_parts: quarterly financial history, kept separate from monthly outcomes.
- valuation_parts: monthly historical valuation data, missing values retained.
- monthly_liquidity_panel.parquet/.csv: security-month liquidity/size/returns and observed membership.
- regression_cross_section.parquet/.csv: current companies with recent completed-month liquidity.
- regression_cross_section_available.parquet/.csv: only current companies with resolved company
  identities and downloaded reference data. Coverage remains incomplete for Russell 2000.
- float_turnover_snapshot_3m_pct uses Bloomberg average 3-month share volume / current public
  float. dollar_volume_proxy_current_price_3m_usd is current price times mean share volume:
  it is explicitly a PROXY, not the actual average traded USD used in the monthly panel.
- research_overview.xlsx: company snapshot, history coverage, field coverage, current memberships.
- cache: complete original request specifications and responses, with retrieval timestamps.
- field_dictionary.csv and field_documentation.json: Bloomberg-provided field definitions.
- api_errors.json: per-security and per-field errors, including unavailable historical fields.
- coverage.csv, field_coverage.csv, coverage_summary.json: actual coverage, including gaps.
- configuration.json, run_status.json and collect.log: requested scope and actual run status.

2026-10-05 collection status
- Bloomberg returned DAILY_CAPACITY_REACHED and collection stopped without bypassing the limit.
- recover_russell_available.py builds PARTIAL exports offline from complete cached responses,
  the earlier same-day Russell analysis and existing financial-history files in data/.
- Reference provenance is in reference_source. membership_only_no_reference rows are missing
  company data, not completed observations. Examine reference_available and company_id_missing.
- Reused annual and quarterly financial histories are in fundamentals_annual_available and
  fundamentals_quarterly_available (.parquet/.csv). They retain source ticker, source file,
  native currency metadata, and a flag that statement vintages are not verified point-in-time.
  Monetary amounts are converted from the existing collector's millions to units, verified
  using Apple's June 2026 quarterly revenue; EPS and percentage ratios are not scaled.
- Reused daily history currently covers July-October 2026, not the requested 2021-2026 window.
  Historical market cap/float/total-return fields missing in that earlier download stay null.
- Existing long financial histories can extend before 2015. They are retained as additional
  available evidence; they do not imply completion of the new requested universe or windows.
- Requested date windows in coverage_summary.json are TARGETS. Actual availability is separately
  reported by dataset, and run_status.json remains incomplete_resumable until collection completes.
- No automatic future collection is scheduled. Resume the collector once capacity is available.

Definitions and regression precautions
- U.S. exchange tickers are mapped to US composite tickers; delisted ticker suffixes are preserved.
- In the checked historical API responses with SCALING_FORMAT=UNT, EQY_SH_OUT is already
  in shares, while EQY_FLOAT remains in millions and is multiplied by 1e6. Reference snapshot
  share fields are in millions. These different scales were checked using AAPL and AA.
  Fundamental amounts use SCALING_FORMAT=UNT; native fundamental currencies are retained.
- Average daily dollar volume uses actual TURNOVER, not closing price times volume.
- Amihud proxy is mean absolute split-adjusted daily return / USD traded in millions.
  It is not a measured execution cost. Dividend-adjusted returns are separate.
- Monthly returns use the total return index; missing index data is not filled with price returns.
  A diagnostic flags series where the returned index exactly matches raw price throughout:
  those series need dividend/return validation before use as verified total-return outcomes.
- Partial months and months with fewer than 15 trading days are flagged. Do not treat October
  2026's two observations as a completed month.
- Historical membership is only observed quarterly. Flags use the last observed quarter-end
  STRICTLY BEFORE each outcome month starts. They are labelled 'observed', and do not establish
  membership on every intervening day or exact announcement/addition/deletion dates.
- Historical member weights returned near zero/nonpositive in initial checks; raw weights
  are retained for auditing but are not used as regression weights or constituent filters.
- Company IDs and industry classifications in merged panels are resolved NOW, not guaranteed
  historical company identities or industry classifications. The industry columns say 'current'.
- Multiple share classes appear in the security-month panel. Select one line per company/month
  or model their dependence; do not count them as independent firms. Current company exports
  aggregate membership by company ID and choose the most active line, preferring an S&P line.
- Historical fundamentals can be restated, and their sampled dates are not verified original
  public release dates. No statement-vintage or publication-date archive is claimed. Do not
  use these unlagged as point-in-time signals for a tradable backtest. They remain separate.
- Bloomberg current company/ticker resolution, missing histories, quarterly membership gaps,
  mergers and ticker reuse can still cause selection/identity issues. Examine the coverage files.
- Russell reconstitution changes from annual to semiannual in 2026; do not assume identical
  event schedules throughout the sample. Source: LSEG Russell US Indexes ground rules and
  https://www.lseg.com/en/ftse-russell/russell-reconstitution
- This collection prepares data; it does not estimate causal effects or verify an arbitrage.

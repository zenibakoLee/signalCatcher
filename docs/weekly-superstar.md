# Weekly Superstar discovery

`python -m pipeline superstar-weekly` runs the bounded US-listed platform-inflection audit. Daily collection, scoring, digest, keyword, and theme/trend work remain in `daily`; daily no longer invokes thesis scouting or writes candidate snapshots.

## Bounds and model policy

- Input: at most 120 scored `raw_items` from the previous 30 days, score 60+, point-in-time at the run cutoff.
- Prefilter: one Luna request, at most 12 company hypotheses, citing only supplied `raw_item_id` values.
- Final synthesis: at most one Terra request and eight candidates. Sol, API keys, Claude, fallback, and application retry are not used.
- Terra output is structurally bounded: six 64-character stage claims with at
  most one citation each, plus at most two sparse filter findings per candidate.
  Each filter finding has one 64-character claim and exactly one citation. Terra
  does not return company names or model-authored gap strings. All claims are
  printable ASCII and all output IDs are bounded to SQLite's signed 64-bit
  maximum. Tickers use a narrow uppercase ASCII ticker pattern.
- Company is resolved deterministically from the validated Luna prefilter map;
  it cannot be rewritten by Terra. Luna company names are printable ASCII and
  at most 120 characters.
- The maximal eight-candidate fixture uses every schema cardinality, 64-character
  claims, maximum IDs, and maximum ticker length. UTF-8 sizes are 14,768 bytes
  compact and 24,464 / 28,698 / 32,932 bytes at indentation 2 / 3 / 4, all below
  the intentional 132,096-byte accepted-response cap. A one-time
  `uv run --with tiktoken` acceptance probe using `o200k_base` measured 3,198
  compact tokens and 5,002 indent-4 tokens, both below 8,000. The automated
  suite keeps exact cardinality checks and an indent-4 size ceiling without
  adding a runtime or test dependency. Pathological unlimited whitespace is
  intentionally not accepted by the byte cap.
- The LLM run ID is isolated as `superstar-weekly-<pipeline_run_id>-<uuid>`.
- SEC work is completed before the short database write transaction. At most eight SEC submissions are checked.

## Hypothesis audit gate

V1 audits whether the bounded article corpus contains exact support for six
platform-stage hypotheses. A model label of `proven` means only "supported by
the supplied corpus". It is not audited primary evidence, a qualification, or
a publication decision.

The six separate hypotheses are:

- `wedge_product`
- `adjacent_products_attach`
- `customer_data_workflow_accumulates`
- `third_party_developers_join`
- `switching_costs_rise`
- `de_facto_standard`

Every supported stage must cite an exact supplied `raw_item_id` and its exact
UTC source date. A raw item may support only one stage per issuer. Unknown
stages are `missing` with no claim or citation. Unsupplied IDs, wrong dates,
cross-stage reuse, and generated evidence are rejected.

## Document-derived v2 filter audit

`config/superstar_filter_v2.json` records the public Google Doc URL and the
version of the implemented contract. V2 is additive: it does not replace the
six platform stages or rewrite historical `weekly_superstar_*` rows.

Post-validation materializes every candidate audit with the nine common
dimensions:

- `self_reinforcing_moat`
- `performance_inflection`
- `platform_product_transition`
- `management_execution`
- `growth_quality`
- `profitability_quality`
- `valuation_hurdle`
- `tam_scalability`
- `competition_durability`

The provider does not return that dense matrix. It returns `industry_track`,
the fixed `classification="unclassified"`, and zero to two sparse
`filter_findings`, limited to `supported` or `partial`. Post-validation then
adds every missing core and applicable industry row in canonical order with an
explicit deterministic evidence gap.

The materialized audit also contains every criterion for one `industry_track`:
`software_platform` (10), `semiconductor_industrial` (12),
`energy_infrastructure` (8), or `other_unclassified` (0). Each applicable
criterion is persisted in order with status `supported`, `partial`, or
`missing` and deterministic explicit evidence gaps. `supported` and `partial`
claims require exactly one candidate-scoped `raw_item_id` and exact
`source_date` citation. The model never authors gap text. `missing` claims are
empty and cannot retain model-authored citations.

Sparse findings are fail-closed rather than run-fatal. Wrong-track,
cross-candidate, wrong-date, unsupported, incomplete, or otherwise unresolved
findings are discarded and become canonical missing rows. Every occurrence of a
duplicated criterion is discarded rather than selecting one. Only an exact
valid citation can produce a supported or partial stored claim. The six
platform stages keep their stricter existing validation and still reject bad
evidence.

The article corpus may support a bounded qualitative claim, but it is not an
authoritative financial, valuation, adoption, or management measurement. The
current writer therefore sets `authoritative_measurements_present=0`, stores
SQL `NULL` for `classification`, and records `classification_status` as
`unclassified`. It never emits a score, rank, recommendation, or price-return
ranking. Valuation and current low GAAP margin remain hurdles rather than
automatic rejection rules. Rows with no self-reinforcing-moat support remain in
the audit tables with `display_ready=0` rather than being deleted.

The source document asks for 8–15 candidates. This system deliberately retains
its hard maximum of eight and permits zero: fixed LLM/network cost bounds and
the fail-closed evidence gate take precedence over filling a quota.

The gate is fail-closed. Even when the model marks all six hypotheses `proven`,
an SEC-verified US listing is stored as `insufficient_evidence` with
`authoritative_stage_measurements` in `missing_evidence`. An issuer without SEC
listing proof remains audit-only as `unverified_us_listing`. V1 always records
`candidates_published=0`; neither status `qualified` nor status `published` can
be emitted.

`coverage_ratio` is the number of corpus-supported stage hypotheses divided by
six. It describes snapshot coverage only. It is not a rank, score, confidence,
probability, or qualification signal. `missing_evidence` records absent stage
hypotheses plus release-gate gaps. The legacy `score` and `rank` columns are
constrained to remain `NULL`.

GO remains blocked until the pipeline has all of the following: deterministic
issuer-to-product mapping; primary-source evidence plus quantified adoption;
workflow and retention measurements; ecosystem/developer mapping; direct
switching-cost evidence; and a measurable standards footprint. The current
article corpus cannot establish these conditions.

## Provenance and retries

`weekly_superstar_snapshots` and `weekly_superstar_stage_evidence` retain the weekly run, as-of date, feature version, exact raw-item foreign keys, source, source date, stage coverage, and missing-evidence contract. The `superstar_weekly` row in `pipeline_runs` records input, considered, published, status, duration, and errors. Same-run persistence is idempotent and preserves the first stored audit and its exact provenance. A provider unknown outcome fails the run; it is not retried or replaced by a fallback model.

## Scheduling

The LaunchAgent uses the macOS host timezone. Set macOS to `Asia/Seoul`, then install:

```bash
sudo systemsetup -settimezone Asia/Seoul   # if the host is not already KST
bash launchd/install.sh install
bash launchd/install.sh status
```

`com.signalcatcher.superstar-weekly.plist` schedules Sunday (`Weekday=0`) at `09:00`. Installation is explicit; repository tests do not load the agent.

Manual dry execution against a non-production test database can use:

```bash
python -m pipeline superstar-weekly --as-of 2026-09-15
```

## Remaining no-go gaps

- A stored article can still repeat another publisher's underlying reporting; the gate rejects duplicate raw IDs and generated citations but does not claim semantic independence across publishers.
- SEC proof establishes an operating issuer with a sole exchange/ticker pair and recent periodic filing. It does not prove common-stock instrument class, liquidity, platform economics, or investability.
- Stage claims are classifications of collected evidence, not audited company facts. Missing direct first-party evidence remains missing.
- No valuation, revenue sensitivity, moat magnitude, return forecast, score, or rank is published by this version.

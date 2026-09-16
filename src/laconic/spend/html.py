"""Self-contained HTML renderer for the validated local evidence payload."""
# ruff: noqa: E501

from __future__ import annotations

import json
from html import escape
from typing import Any

from laconic.spend.privacy import validate_report_json
from laconic.spend.report import (
    FALLBACK_SHARE_QUOTABLE_MAX_PCT,
    SpendReport,
    limitation_prose,
)


def _e(value: object) -> str:
    return escape(str(value), quote=True)


def _integer(value: Any) -> str:
    return f"{int(value):,}"


def _usd(value: Any) -> str:
    return f"${float(value):,.2f}"


def _pct(value: Any) -> str:
    return f"{float(value):.2f}%"


def _ratio(numerator: int, denominator: int) -> float:
    return 0.0 if denominator == 0 else 100.0 * numerator / denominator


def _cost_segments(shares: dict[str, Any] | None) -> str:
    values = shares or {
        "uncached_input": 0.0,
        "cache_read": 0.0,
        "cache_write": 0.0,
        "output": 0.0,
    }
    colors = {
        "uncached_input": "var(--ink)",
        "cache_read": "var(--signal)",
        "cache_write": "var(--red)",
        "output": "var(--sand-dark)",
    }
    return "".join(
        '<span class="segment" style="width:'
        f"{float(values[key]):.6f}%;background:{colors[key]}"
        '"></span>'
        for key in ("uncached_input", "cache_read", "cache_write", "output")
    )


def _estimate_panel(estimate: dict[str, Any] | None) -> str:
    if estimate is None:
        return """
          <section class="estimate-card unavailable" aria-labelledby="estimate-title">
            <p class="eyebrow">Modelled cost avoided</p>
            <h2 id="estimate-title">Not estimable from this snapshot</h2>
            <p>No removed characters or cached tokens were available to price. This is not a zero.</p>
            <span class="basis">modelled_not_measured</span>
          </section>
        """
    fallback_share = float(estimate["fallback_priced_cost_share_pct"])
    dollars = (
        f"<strong>{_usd(estimate['avoided_cost_usd_low'])}–"
        f"{_usd(estimate['avoided_cost_usd_high'])}</strong>"
        if fallback_share <= FALLBACK_SHARE_QUOTABLE_MAX_PCT
        else "<strong>Dollar band withheld</strong>"
    )
    withheld = (
        ""
        if fallback_share <= FALLBACK_SHARE_QUOTABLE_MAX_PCT
        else (
            '<p class="withheld">The dollar band is not quotable because '
            f"{fallback_share:.1f}% of its pricing denominator uses fallback rates.</p>"
        )
    )
    return f"""
      <section class="estimate-card" aria-labelledby="estimate-title">
        <div>
          <p class="eyebrow">Modelled cost avoided</p>
          <h2 id="estimate-title">{dollars}</h2>
          <p class="share-band">{_pct(estimate["avoided_share_pct_low"])}–{_pct(estimate["avoided_share_pct_high"])} of the matched modelled bill</p>
          {withheld}
        </div>
        <div class="estimate-proof">
          <span class="basis">{_e(estimate["basis"])}</span>
          <p>Single arm. No codec-off counterfactual exists. This band is a model, not a measurement.</p>
          <dl>
            <div><dt>Denominator</dt><dd>{_usd(estimate["denominator_usd"])}</dd></div>
            <div><dt>Characters avoided</dt><dd>{_integer(estimate["chars_avoided"])}</dd></div>
            <div><dt>Assumed chars / token</dt><dd>{_e(estimate["chars_per_token_low"])}–{_e(estimate["chars_per_token_high"])}</dd></div>
            <div><dt>Measured cache re-read rate</dt><dd>{float(estimate["cache_reread_multiplier"]):,.1f}×</dd></div>
          </dl>
        </div>
      </section>
    """


def render_html(report: SpendReport, *, synthetic: bool = False) -> str:
    """Render one validated report without filesystem, clock, or network access."""
    payload = json.loads(report.to_json())
    validate_report_json(payload)
    codec = payload["codec"]
    estimate = payload["estimate"]
    shares = payload["corpus_shares"]
    emission_pct = _ratio(codec["emitted"], codec["eligible"])
    visible_pct = _ratio(codec["visible_chars"], codec["raw_chars"])
    marker = '<span class="fixture-marker">synthetic fixture</span>' if synthetic else ""
    limitations = "".join(
        f"<li><code>{_e(name)}</code><span>{_e(limitation_prose(name))}</span></li>"
        for name in payload["limitations"]
    )
    cost_rows = "".join(
        f'<tr><th scope="row">{label}</th><td>{_usd(payload["corpus_cost"][key])}</td>'
        f"<td>{'n/a' if shares is None else _pct(shares[key])}</td></tr>"
        for key, label in (
            ("uncached_input", "Uncached input"),
            ("cache_read", "Cache read"),
            ("cache_write", "Cache write"),
            ("output", "Output"),
        )
    )
    unpriced = "".join(f"<li><code>{_e(name)}</code></li>" for name in payload["unpriced_models"])
    unpriced_panel = (
        f'<aside class="warning"><h3>Unpriced models</h3><ul>{unpriced}</ul></aside>'
        if unpriced
        else ""
    )
    unknown = "".join(f"<li><code>{_e(name)}</code></li>" for name in payload["unknown_usage_keys"])
    unknown_panel = (
        f'<aside class="warning"><h3>Unknown usage keys</h3><ul>{unknown}</ul></aside>'
        if unknown
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">
  <title>Laconic evidence ledger</title>
  <style>
    :root {{
      color-scheme: light;
      --paper:#f0eadf;--paper-deep:#dfd5c5;--ink:#191816;--muted:#69635b;
      --rule:#b9ae9d;--red:#a33a2d;--red-soft:#ead3cd;--signal:#183c3b;
      --signal-ink:#dce9df;--sand-dark:#9d8f79;--white:#fffdf8;
      --serif:Georgia,'Times New Roman',serif;--mono:Menlo,Monaco,'Courier New',monospace;
    }}
    *{{box-sizing:border-box}} html{{background:var(--paper);color:var(--ink)}}
    body{{margin:0;font-family:var(--serif);background:linear-gradient(90deg,transparent 0 7.5%,rgba(163,58,45,.07) 7.5% 7.65%,transparent 7.65%),var(--paper);line-height:1.45}}
    body:before{{content:'';position:fixed;inset:0;pointer-events:none;opacity:.18;background-image:repeating-linear-gradient(0deg,transparent 0 3px,rgba(25,24,22,.08) 4px);mix-blend-mode:multiply}}
    .ledger{{position:relative;max-width:1180px;margin:auto;padding:52px 42px 72px}}
    header{{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:32px;border-top:4px solid var(--ink);border-bottom:1px solid var(--rule);padding:26px 0 28px}}
    .kicker,.eyebrow,.provenance dt,.basis,.fixture-marker{{font:700 12px/1.3 var(--mono);letter-spacing:.13em;text-transform:uppercase}}
    h1{{font-size:clamp(48px,8vw,104px);font-weight:400;line-height:.82;letter-spacing:-.055em;margin:12px 0 18px;max-width:760px}}
    .lede{{font-size:clamp(18px,2vw,25px);max-width:700px;margin:0;color:#39352f}}
    .claim-stamp{{align-self:end;width:210px;border:2px solid var(--red);color:var(--red);padding:18px;transform:rotate(-1.5deg);font:700 12px/1.5 var(--mono);text-transform:uppercase}}
    .fixture-marker{{display:inline-block;background:var(--red);color:var(--white);padding:7px 10px;margin-top:16px}}
    .provenance{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:0;border-bottom:1px solid var(--rule)}}
    .provenance div{{padding:16px 14px 18px 0;border-right:1px solid var(--rule)}} .provenance div+div{{padding-left:14px}}
    .provenance dt{{color:var(--muted)}} .provenance dd{{margin:6px 0 0;font:13px/1.4 var(--mono);overflow-wrap:anywhere}}
    .section-title{{display:flex;align-items:baseline;justify-content:space-between;gap:20px;margin:54px 0 18px;border-bottom:1px solid var(--ink)}}
    .section-title h2{{font-size:34px;font-weight:400;margin:0 0 8px}} .section-title span{{font:12px var(--mono);color:var(--muted)}}
    .evidence-grid{{display:grid;grid-template-columns:repeat(12,1fr);gap:12px}}
    .card{{background:rgba(255,253,248,.58);border:1px solid var(--rule);min-height:168px;padding:18px;display:flex;flex-direction:column;justify-content:space-between}}
    .card:nth-child(1),.card:nth-child(2){{grid-column:span 3}} .card:nth-child(3),.card:nth-child(4){{grid-column:span 3}}
    .card .value{{font-size:44px;line-height:1;letter-spacing:-.04em}} .card .label{{font:12px var(--mono);text-transform:uppercase;color:var(--muted)}}
    .card p{{margin:10px 0 0;color:var(--muted);font-size:14px}}
    .signal-band{{margin:58px 0;padding:50px max(42px,calc((100% - 1096px)/2));background:var(--signal);color:var(--signal-ink);display:grid;grid-template-columns:.78fr 1.22fr;gap:60px;overflow:hidden}}
    .signal-band h2{{font-size:46px;font-weight:400;line-height:1;margin:10px 0 18px}} .signal-band p{{max-width:500px}}
    .signal-graphic{{position:relative;min-height:245px;border-left:1px solid rgba(220,233,223,.34);padding:18px 0 0 34px}}
    .signal-graphic svg{{width:100%;height:auto;display:block}} .signal-graphic text{{fill:var(--signal-ink);font:13px var(--mono)}} .signal-graphic .frame{{fill:none;stroke:rgba(220,233,223,.38)}} .signal-graphic .cut{{stroke:#d98c78;stroke-width:3}}
    .signal-note{{font:12px/1.5 var(--mono);color:#a9c2b8}}
    .estimate-card{{border-top:5px solid var(--red);border-bottom:1px solid var(--rule);padding:28px 0;display:grid;grid-template-columns:1.2fr .8fr;gap:56px}}
    .estimate-card h2{{font-size:clamp(38px,6vw,72px);line-height:.95;font-weight:400;margin:10px 0}} .estimate-card h2 strong{{font-weight:400}}
    .share-band{{font-size:20px}} .basis{{display:inline-block;background:var(--red-soft);color:var(--red);padding:7px 9px}}
    .estimate-proof{{border-left:1px solid var(--rule);padding-left:28px}} .estimate-proof dl{{margin:18px 0 0}}
    .estimate-proof dl div{{display:flex;justify-content:space-between;gap:12px;border-top:1px solid var(--rule);padding:8px 0}}
    .estimate-proof dt{{color:var(--muted)}} .estimate-proof dd{{font-family:var(--mono);margin:0;text-align:right}}
    .withheld,.warning{{border-left:3px solid var(--red);padding-left:12px;color:var(--red)}}
    .composition{{display:grid;grid-template-columns:1.15fr .85fr;gap:42px}} .bar{{height:28px;display:flex;border:1px solid var(--ink);overflow:hidden}}
    .segment{{display:block;height:100%}} table{{border-collapse:collapse;width:100%;margin-top:20px}} th,td{{border-bottom:1px solid var(--rule);padding:9px 0;text-align:right}} th{{text-align:left;font-weight:400}} td{{font-family:var(--mono)}}
    .codec-plot{{background:var(--white);border:1px solid var(--rule);padding:20px}} .meter{{height:22px;background:var(--paper-deep);position:relative;margin:10px 0 20px}}
    .meter span{{display:block;height:100%;background:var(--red)}} .meter.visible span{{background:var(--signal)}}
    .meter-label{{display:flex;justify-content:space-between;font:12px var(--mono)}}
    .limits{{columns:2;column-gap:42px;padding:0;list-style:none}} .limits li{{break-inside:avoid;border-top:1px solid var(--rule);padding:12px 0 18px}}
    .limits code{{display:block;color:var(--red);font:12px/1.4 var(--mono);overflow-wrap:anywhere;margin-bottom:7px}} .limits span{{color:#49443e}}
    .warning{{margin:20px 0}} .warning h3{{margin:0}} .warning ul{{margin-bottom:0}}
    footer{{margin-top:58px;padding-top:22px;border-top:4px solid var(--ink);display:grid;grid-template-columns:1fr 2fr;gap:32px}}
    footer code{{font:12px/1.5 var(--mono);overflow-wrap:anywhere}} footer p{{margin:0;color:var(--muted)}}
    @media(max-width:800px){{.ledger{{padding:30px 20px 52px}}header,.signal-band,.estimate-card,.composition,footer{{grid-template-columns:1fr}}.claim-stamp{{width:auto}}.provenance{{grid-template-columns:1fr 1fr}}.provenance div{{border-bottom:1px solid var(--rule)}}.card:nth-child(n){{grid-column:span 6}}.signal-band{{padding:38px 20px;margin:42px 0}}.limits{{columns:1}}}}
    @media(max-width:480px){{h1{{font-size:54px}}.provenance{{grid-template-columns:1fr}}.card:nth-child(n){{grid-column:span 12}}.signal-band h2{{font-size:36px}}}}
    @media(prefers-reduced-motion:reduce){{*{{scroll-behavior:auto}}}}
    @media print{{body{{background:#fff}}body:before{{display:none}}.signal-band{{margin-left:0;margin-right:0}}}}
  </style>
</head>
<body>
  <main>
  <div class="ledger">
    <header>
      <div>
        <p class="kicker">Laconic / local evidence ledger</p>
        <h1>Context has a cost.</h1>
        <p class="lede">A content-free account of where modelled spend accumulated and what the local runtime codec did in the same sessions.</p>
        {marker}
      </div>
      <aside class="claim-stamp" aria-label="Claim boundary">No savings claim<br>No counterfactual<br>Single-arm evidence</aside>
    </header>

    <dl class="provenance" aria-label="Report provenance">
      <div><dt>Report</dt><dd>schema v{_e(payload["schema_version"])}</dd></div>
      <div><dt>Laconic</dt><dd>v{_e(payload["laconic_version"])}</dd></div>
      <div><dt>Basis</dt><dd>{_e(payload["generation_basis"])}</dd></div>
      <div><dt>Freshness</dt><dd>{_e(payload["source_freshness"])}</dd></div>
      <div><dt>Privacy</dt><dd>{_e(payload["privacy_status"])}</dd></div>
    </dl>

    <section aria-labelledby="evidence-title">
      <div class="section-title"><h2 id="evidence-title">Evidence at a glance</h2><span>consumer-visible facts</span></div>
      <div class="evidence-grid">
        <article class="card"><span class="label">Priced sessions</span><strong class="value">{_integer(payload["sessions_with_spend"])}</strong><p>{_integer(payload["root_sessions"])} root · {_integer(payload["nested_sessions"])} subagent</p></article>
        <article class="card"><span class="label">Matched sessions</span><strong class="value">{_integer(payload["matched_sessions"])}</strong><p>{_integer(payload["unmatched_spend_sessions"])} spend-only · {_integer(payload["unmatched_ledger_sessions"])} ledger-only</p></article>
        <article class="card"><span class="label">Eligible observations</span><strong class="value">{_integer(codec["eligible"])}</strong><p>{_integer(codec["emitted"])} replaced · {_integer(codec["pass_through"])} passed through</p></article>
        <article class="card"><span class="label">Ledger health</span><strong class="value">{_integer(payload["damaged_ledgers"])}</strong><p>damaged ledgers · {_integer(codec["full_expansions"])} full / {_integer(codec["span_expansions"])} span expansions</p></article>
      </div>
    </section>
  </div>

  <section class="signal-band" aria-labelledby="signal-title">
    <div>
      <p class="eyebrow">Context signal / explanatory model</p>
      <h2 id="signal-title">Excluded once. Absent later.</h2>
      <p>Tool output removed before context entry is not resident in later turns. This diagram explains the mechanism; it is not a measured causal curve.</p>
      <p class="signal-note">Measured per-session cache residency is not present in this payload.</p>
    </div>
    <div class="signal-graphic">
      <svg viewBox="0 0 640 250" role="img" aria-labelledby="context-signal-title context-signal-description">
        <title id="context-signal-title">Excluded context remains absent from later turns</title>
        <desc id="context-signal-description">Four schematic context bars with an increasing hatched region. The hatching explains absence and is not measured volume.</desc>
        <defs><pattern id="absence-hatch" width="12" height="12" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="12" stroke="rgba(220,233,223,.28)" stroke-width="2"/></pattern></defs>
        <g transform="translate(0 8)">
          <rect class="frame" x="0" y="0" width="620" height="38"/><text x="14" y="24">turn 01 · context enters</text><rect x="484" y="0" width="136" height="38" fill="url(#absence-hatch)"/><line class="cut" x1="484" y1="0" x2="484" y2="38"/>
          <rect class="frame" x="0" y="56" width="620" height="38"/><text x="14" y="80">turn 02 · earlier output excluded</text><rect x="360" y="56" width="260" height="38" fill="url(#absence-hatch)"/><line class="cut" x1="360" y1="56" x2="360" y2="94"/>
          <rect class="frame" x="0" y="112" width="620" height="38"/><text x="14" y="136">turn 03 · exclusion compounds</text><rect x="242" y="112" width="378" height="38" fill="url(#absence-hatch)"/><line class="cut" x1="242" y1="112" x2="242" y2="150"/>
          <rect class="frame" x="0" y="168" width="620" height="38"/><text x="14" y="192">turn 04 · context stays absent</text><rect x="136" y="168" width="484" height="38" fill="url(#absence-hatch)"/><line class="cut" x1="136" y1="168" x2="136" y2="206"/>
          <text x="0" y="236" opacity=".7">hatched region = explanatory absence, not measured volume</text>
        </g>
      </svg>
    </div>
  </section>

  <div class="ledger">
    {_estimate_panel(estimate)}

    <section aria-labelledby="composition-title">
      <div class="section-title"><h2 id="composition-title">Composition, not causation</h2><span>same canonical payload</span></div>
      <div class="composition">
        <div>
          <p class="eyebrow">Whole-corpus modelled cost</p>
          <div class="bar" role="img" aria-label="Cost composition by token category">{_cost_segments(shares)}</div>
          <table>
            <caption class="eyebrow">Modelled token-cost composition</caption>
            <thead><tr><th scope="col">Component</th><th scope="col">USD</th><th scope="col">Share</th></tr></thead>
            <tbody>{cost_rows}<tr><th scope="row">Total</th><td>{_usd(payload["corpus_cost"]["total"])}</td><td>100%</td></tr></tbody>
          </table>
        </div>
        <div class="codec-plot">
          <p class="eyebrow">Runtime codec</p>
          <div class="meter-label"><span>Emission</span><span>{emission_pct:.2f}%</span></div>
          <div class="meter" aria-label="Emission rate"><span style="width:{emission_pct:.6f}%"></span></div>
          <div class="meter-label"><span>Visible characters</span><span>{visible_pct:.2f}% of raw</span></div>
          <div class="meter visible" aria-label="Visible character share"><span style="width:{visible_pct:.6f}%"></span></div>
          <dl class="estimate-proof">
            <div><dt>Raw</dt><dd>{_integer(codec["raw_chars"])}</dd></div>
            <div><dt>Visible</dt><dd>{_integer(codec["visible_chars"])}</dd></div>
            <div><dt>Avoided</dt><dd>{_integer(codec["chars_avoided"])}</dd></div>
          </dl>
        </div>
      </div>
      {unpriced_panel}{unknown_panel}
    </section>

    <section aria-labelledby="limits-title">
      <div class="section-title"><h2 id="limits-title">Claim boundary</h2><span>closed limitation vocabulary</span></div>
      <ul class="limits">{limitations}</ul>
    </section>

    <footer>
      <div><p class="eyebrow">Canonical evidence</p><code>sha256:{_e(report.sha256)}</code></div>
      <p>Generated from local read-only aggregate sources. The HTML contains inline CSS and SVG only: no script, remote asset, telemetry, or network request. The canonical JSON is the semantic source for this page.</p>
    </footer>
  </div>
  </main>
</body>
</html>
"""

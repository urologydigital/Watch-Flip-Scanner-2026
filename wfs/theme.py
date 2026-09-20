"""Premium Ethereum visual system for the Phase 5 dashboard (spec s.24).

Warm Ivory page, white surfaces, Royal Indigo reserved for genuine emphasis.
Verdict colours are used sparingly and only where they aid a fast decision.
"""
from __future__ import annotations

from .flip_analysis import FlipAnalysis
from .flip import BASE, PATIENT, QUICK, DaysToSell

TOKENS = {
    "bg": "#F7F6F2",
    "surface": "#FFFFFF",
    "text_primary": "#202024",
    "text_secondary": "#696970",
    "brand_primary": "#5146B8",
    "brand_secondary": "#756CB8",
    "accent_lavender": "#E9E5F5",
    "accent_ice": "#E5EDF4",
    "accent_blush": "#F1E8E3",
    "border": "#DEDDD8",
    "accent_electric": "#536DFE",
}

VERDICT_STYLE = {
    "BUY": {"fg": "#1B7A4B", "bg": "#E6F4EC", "label": "BUY"},
    # Phase 5.2: visually distinct from WATCH — it uses the brand indigo, because
    # it is a prompt to act (go and check), not a verified opportunity.
    "CROSS-CHECK": {"fg": "#5146B8", "bg": "#E9E5F5", "label": "CROSS-CHECK"},
    "WATCH": {"fg": "#8A6100", "bg": "#FBF0DC", "label": "WATCH"},
    "PASS": {"fg": "#9B3232", "bg": "#F7E7E7", "label": "PASS"},
}

CSS = f"""
<style>
:root {{
  --bg: {TOKENS['bg']};
  --surface: {TOKENS['surface']};
  --text-primary: {TOKENS['text_primary']};
  --text-secondary: {TOKENS['text_secondary']};
  --brand-primary: {TOKENS['brand_primary']};
  --brand-secondary: {TOKENS['brand_secondary']};
  --accent-lavender: {TOKENS['accent_lavender']};
  --accent-ice: {TOKENS['accent_ice']};
  --accent-blush: {TOKENS['accent_blush']};
  --border: {TOKENS['border']};
  --accent-electric: {TOKENS['accent_electric']};
}}

.stApp {{ background: var(--bg); }}

html, body, [class*="css"] {{
  font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color: var(--text-primary);
}}

h1, h2, h3, h4 {{ color: var(--text-primary); letter-spacing: -0.01em; }}

.wfs-card {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 20px 22px;
  margin-bottom: 14px;
  box-shadow: 0 1px 2px rgba(32,32,36,0.04);
}}

.wfs-head {{
  display: flex; justify-content: space-between;
  align-items: flex-start; gap: 16px; margin-bottom: 6px;
}}

.wfs-title {{ font-size: 1.02rem; font-weight: 650; color: var(--text-primary); }}
.wfs-ref {{ font-size: 0.8rem; color: var(--text-secondary); margin-top: 2px; }}

.wfs-badge {{
  display: inline-block; padding: 5px 13px; border-radius: 999px;
  font-size: 0.74rem; font-weight: 700; letter-spacing: 0.05em; white-space: nowrap;
}}

.wfs-flip {{
  font-size: 1.7rem; font-weight: 700; color: var(--brand-primary);
  line-height: 1; margin-top: 4px;
}}
.wfs-flip-label {{
  font-size: 0.66rem; color: var(--text-secondary);
  text-transform: uppercase; letter-spacing: 0.09em;
}}

.wfs-grid {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(112px, 1fr));
  gap: 10px; margin: 16px 0 4px 0;
}}
.wfs-metric {{
  background: var(--accent-ice); border-radius: 10px; padding: 10px 12px;
}}
.wfs-metric.lav {{ background: var(--accent-lavender); }}
.wfs-metric.blush {{ background: var(--accent-blush); }}
.wfs-metric-label {{
  font-size: 0.63rem; color: var(--text-secondary);
  text-transform: uppercase; letter-spacing: 0.07em;
}}
.wfs-metric-value {{
  font-size: 1.0rem; font-weight: 650; color: var(--text-primary); margin-top: 3px;
}}
.wfs-metric-sub {{ font-size: 0.7rem; color: var(--text-secondary); margin-top: 1px; }}

table.wfs-strategy {{
  width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 0.83rem;
}}
table.wfs-strategy th {{
  text-align: left; font-weight: 600; color: var(--text-secondary);
  font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.06em;
  padding: 6px 8px; border-bottom: 1px solid var(--border);
}}
table.wfs-strategy td {{
  padding: 7px 8px; border-bottom: 1px solid var(--border);
  color: var(--text-primary);
}}
table.wfs-strategy tr:last-child td {{ border-bottom: none; }}
table.wfs-strategy td.pos {{ color: #1B7A4B; font-weight: 600; }}
table.wfs-strategy td.neg {{ color: #9B3232; font-weight: 600; }}

.wfs-why {{
  background: var(--accent-lavender); border-left: 3px solid var(--brand-primary);
  border-radius: 8px; padding: 12px 14px; margin-top: 14px;
  font-size: 0.85rem; color: var(--text-primary); line-height: 1.5;
}}
.wfs-why-label {{
  font-size: 0.65rem; font-weight: 700; color: var(--brand-primary);
  text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 5px;
}}

.wfs-links {{
  display: flex; gap: 8px; flex-wrap: wrap; margin-top: 14px;
  padding-top: 12px; border-top: 1px solid var(--border);
}}
.wfs-link {{
  display: inline-block; padding: 7px 14px; border-radius: 8px;
  font-size: 0.78rem; font-weight: 600; text-decoration: none;
  border: 1px solid var(--brand-primary); color: var(--brand-primary);
  background: var(--surface);
}}
.wfs-link.primary {{
  background: var(--brand-primary); color: #FFFFFF; border-color: var(--brand-primary);
}}
.wfs-link:hover {{ background: var(--accent-lavender); }}
.wfs-link.primary:hover {{ background: var(--brand-secondary); color: #FFFFFF; }}

.wfs-active {{
  background: var(--accent-ice); border-radius: 8px; padding: 11px 14px;
  margin-top: 12px; font-size: 0.83rem;
}}
.wfs-active .tag {{
  font-size: 0.62rem; font-weight: 700; letter-spacing: 0.07em;
  text-transform: uppercase; color: var(--text-secondary); display: block;
  margin-bottom: 4px;
}}
.wfs-discount {{ color: #1B7A4B; font-weight: 700; }}

.wfs-card.pass {{ opacity: 0.72; }}

.wfs-offer {{
  background: var(--accent-blush); border-radius: 8px; padding: 10px 13px;
  margin-top: 10px; font-size: 0.84rem;
}}

.wfs-unverified {{
  display: inline-block; background: #F7E7E7; color: #9B3232;
  font-size: 0.66rem; font-weight: 700; padding: 3px 9px;
  border-radius: 5px; letter-spacing: 0.05em; margin-left: 8px;
}}

.wfs-maxbuy {{
  display: flex; gap: 20px; margin-top: 14px; padding-top: 12px;
  border-top: 1px solid var(--border); flex-wrap: wrap;
}}
.wfs-mb-item {{ font-size: 0.82rem; }}
.wfs-mb-label {{
  font-size: 0.63rem; color: var(--text-secondary);
  text-transform: uppercase; letter-spacing: 0.07em;
}}
.wfs-mb-value {{ font-weight: 650; color: var(--text-primary); }}
.wfs-mb-value.muted {{ color: var(--text-secondary); font-weight: 500; }}

div.stButton > button[kind="primary"] {{
  background: var(--brand-primary); border: none; border-radius: 9px;
  font-weight: 600; color: #FFFFFF;
}}
div.stButton > button[kind="secondary"] {{
  background: var(--surface); border: 1px solid var(--brand-primary);
  color: var(--brand-primary); border-radius: 9px; font-weight: 600;
}}
[data-testid="stMetricValue"] {{ color: var(--text-primary); font-weight: 650; }}
</style>
"""


def _money(value: float | None) -> str:
    return f"£{value:,.0f}" if value is not None else "n/a"


def render_card(a: FlipAnalysis) -> str:
    """Full opportunity card as HTML (spec s.15)."""
    style = VERDICT_STYLE.get(a.verdict, VERDICT_STYLE["PASS"])
    low, mid, high = a.evidence.valuation_band()

    unverified = ('<span class="wfs-unverified">UNVERIFIED</span>'
                  if a.evidence.unverified else "")
    market = (f"{_money(low)}–{_money(high)}" if mid is not None
              else "Insufficient Market Evidence")

    sold_line = (f"{a.evidence.sold_count_90d} / 90 days"
                 if a.evidence.has_sold_evidence else "none observed")

    strategy_rows = ""
    for name in (QUICK, BASE, PATIENT):
        s = a.strategies.get(name)
        if not s:
            continue
        cls = "pos" if s.net_profit > 0 else "neg"
        roi = f"{s.net_roi_pct:.0f}%" if s.net_roi_pct is not None else "n/a"
        strategy_rows += (
            f"<tr><td><strong>{name.title()}</strong></td>"
            f"<td>{_money(s.sale_price)}</td>"
            f"<td>{DaysToSell.format(s.days)}</td>"
            f"<td class='{cls}'>{_money(s.net_profit)}</td>"
            f"<td>{roi}</td></tr>"
        )
    strategy_table = (
        "<table class='wfs-strategy'><tr><th>Strategy</th><th>Sale price</th>"
        "<th>Time to sell</th><th>Net profit</th><th>ROI</th></tr>"
        f"{strategy_rows}</table>" if strategy_rows else
        "<div class='wfs-metric-sub' style='margin-top:12px'>"
        "No resale scenarios — insufficient market evidence.</div>"
    )

    mb = a.max_buy
    aggressive = (f"<span class='wfs-mb-value'>{_money(mb.aggressive)}</span>"
                  if mb.aggressive else
                  "<span class='wfs-mb-value muted'>not permitted</span>")

    # --- Phase 5.2: active market benchmark -------------------------------
    active_html = ""
    bench = getattr(a, "benchmark", None)
    if bench is not None and bench.is_usable:
        discount = bench.discount_to_median_pct(a.acquisition)
        disc_html = (f" &nbsp;·&nbsp; <span class='wfs-discount'>"
                     f"{discount:.1f}% below median</span>"
                     if discount and discount > 0 else "")
        active_html = (
            f"<div class='wfs-active'><span class='tag'>eBay UK active asking market "
            f"— not sold evidence</span>"
            f"{bench.active_listing_count} listing(s) &nbsp;·&nbsp; median "
            f"{_money(bench.active_median_price)} &nbsp;·&nbsp; range "
            f"{_money(bench.active_low_price)}–{_money(bench.active_high_price)}"
            f"{disc_html}</div>")

    # --- Phase 5.2: research links ----------------------------------------
    links_html = ""
    links = getattr(a, "links", None)
    if links is not None:
        buttons = "".join(
            f"<a class='wfs-link{" primary" if i == 0 and links.listing else ""}' "
            f"href='{url}' target='_blank' rel='noopener noreferrer'>{label}</a>"
            for i, (label, url) in enumerate(links.buttons()))
        links_html = f"<div class='wfs-links'>{buttons}</div>"

    offer_html = ""
    if a.decision.target_offer:
        o = a.decision.target_offer
        offer_html = (f"<div class='wfs-offer'><strong>Suggested offer: "
                      f"{_money(o.low)}–{_money(o.high)}</strong><br>"
                      f"<span style='color:{TOKENS['text_secondary']}'>{o.rationale}</span></div>")

    # CROSS-CHECK explains itself from the active-market comparison, since it
    # has no confirmed valuation to reason from.
    cc = getattr(a, "cross_check", None)
    if a.verdict == "CROSS-CHECK" and cc is not None:
        why_text = (cc.summary + " Use the links below to verify the market "
                    "yourself before considering an offer.")
    else:
        why_text = a.decision.why

    card_class = "wfs-card pass" if a.verdict == "PASS" else "wfs-card"

    return f"""
<div class="{card_class}">
  <div class="wfs-head">
    <div>
      <div class="wfs-title">{(a.listing.get('brand') or '')} {(a.listing.get('model') or '')}</div>
      <div class="wfs-ref">Ref {a.ref.reference} &nbsp;·&nbsp; Listed {_money(a.listing.get('price'))}
        &nbsp;·&nbsp; Market {market}{unverified}</div>
    </div>
    <div style="text-align:right">
      <span class="wfs-badge" style="color:{style['fg']};background:{style['bg']}">{style['label']}</span>
      <div class="wfs-flip">{a.flip_score.score:.0f}</div>
      <div class="wfs-flip-label">Flip Score</div>
    </div>
  </div>

  <div class="wfs-grid">
    <div class="wfs-metric lav">
      <div class="wfs-metric-label">Confidence</div>
      <div class="wfs-metric-value">{a.confidence.score:.0f}</div>
      <div class="wfs-metric-sub">{a.confidence.band.title()}</div>
    </div>
    <div class="wfs-metric">
      <div class="wfs-metric-label">Liquidity</div>
      <div class="wfs-metric-value">{a.liquidity.score:.0f}</div>
      <div class="wfs-metric-sub">{a.liquidity.band.title()}</div>
    </div>
    <div class="wfs-metric">
      <div class="wfs-metric-label">Velocity</div>
      <div class="wfs-metric-value">{a.velocity.score:.0f}</div>
      <div class="wfs-metric-sub">{(str(int(a.velocity.expected_holding_days)) + 'd hold') if a.velocity.expected_holding_days else 'unknown'}</div>
    </div>
    <div class="wfs-metric blush">
      <div class="wfs-metric-label">Risk</div>
      <div class="wfs-metric-value">{a.risk.total:.0f}</div>
      <div class="wfs-metric-sub">{a.risk.band.title()}</div>
    </div>
    <div class="wfs-metric">
      <div class="wfs-metric-label">UK sold</div>
      <div class="wfs-metric-value">{sold_line}</div>
      <div class="wfs-metric-sub">{a.evidence.level_label}</div>
    </div>
    <div class="wfs-metric">
      <div class="wfs-metric-label">Condition</div>
      <div class="wfs-metric-value">{a.condition.condition_score:.0f}</div>
      <div class="wfs-metric-sub">{a.condition.completeness_label}</div>
    </div>
  </div>

  {active_html}

  {strategy_table}

  <div class="wfs-maxbuy">
    <div class="wfs-mb-item"><div class="wfs-mb-label">Conservative MAX BUY</div>
      <span class="wfs-mb-value">{_money(mb.conservative)}</span></div>
    <div class="wfs-mb-item"><div class="wfs-mb-label">Standard MAX BUY</div>
      <span class="wfs-mb-value">{_money(mb.standard)}</span></div>
    <div class="wfs-mb-item"><div class="wfs-mb-label">Aggressive MAX BUY</div>
      {aggressive}</div>
  </div>

  {offer_html}

  <div class="wfs-why">
    <div class="wfs-why-label">Why {a.verdict}</div>
    {why_text}
  </div>

  {links_html}
</div>
"""

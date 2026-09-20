"""Render the real Phase 5.1.1 dashboard cards to a standalone HTML preview.

Uses the actual wfs/theme.py CSS and render_card() — this is what the Streamlit
dashboard produces, not a mockup.

*** MOCK DATA — the sale counts and prices below are invented for demonstration.
"""
from __future__ import annotations

from wfs import theme
from wfs.flip_analysis import rank
from sample_output import build_cases

BANNER = """
<div class="wfs-banner">
  <strong>Watch Flip Scanner UK — Phase 5.1.1</strong>
  <span>Economics: UK Private Seller · 0.00% platform fees · £32 seller costs per sale</span>
  <em>MOCK DATA — sale counts and prices below are invented for demonstration.</em>
</div>
"""

EXTRA_CSS = """
<style>
body { background: #F7F6F2; margin: 0; padding: 28px 20px 60px;
       font-family: Inter, -apple-system, "Segoe UI", sans-serif; }
.wrap { max-width: 880px; margin: 0 auto; }
.wfs-banner { background: #FFFFFF; border: 1px solid #DEDDD8; border-radius: 14px;
  padding: 16px 20px; margin-bottom: 18px; display: flex; flex-direction: column;
  gap: 5px; }
.wfs-banner strong { font-size: 1.05rem; color: #202024; }
.wfs-banner span { font-size: 0.82rem; color: #5146B8; }
.wfs-banner em { font-size: 0.76rem; color: #9B3232; font-style: normal;
  font-weight: 600; }
.rankbar { background:#E9E5F5; border-radius:10px; padding:12px 16px;
  margin-bottom:18px; font-size:0.82rem; color:#202024; }
.rankbar b { color:#5146B8; }
</style>
"""


def main() -> None:
    cases = rank(build_cases())
    rows = " · ".join(
        f"{i}. {a.verdict} {a.ref.reference} (Flip {a.flip_score.score:.0f})"
        for i, a in enumerate(cases, 1))
    body = "".join(theme.render_card(a) for a in cases)

    html = (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>Watch Flip Scanner UK — dashboard preview</title>"
            f"{theme.CSS}{EXTRA_CSS}</head><body><div class='wrap'>{BANNER}"
            f"<div class='rankbar'>Ranked by <b>Best Flip Opportunities</b>: "
            f"{rows}</div>{body}</div></body></html>")

    out = "/mnt/user-data/outputs/dashboard-preview.html"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"Wrote {out} ({len(html):,} bytes, {len(cases)} cards)")


if __name__ == "__main__":
    main()

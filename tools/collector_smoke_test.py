"""Live collector smoke test — run this on YOUR machine, not in CI.

    python tools/collector_smoke_test.py
    python tools/collector_smoke_test.py --reference 79030N --brand Tudor

This performs a REAL network check against each external source for ONE
reference and prints exactly what came back. It is the only way to know whether
a collector works, because a passing mocked test proves the parser handles the
HTML we imagined, not the HTML the site actually serves.

What it does:
  * reads each site's robots.txt and reports the real verdict
  * attempts one throttled fetch per enabled source
  * reports HTTP status, bytes fetched, records parsed and price certainty
  * never bypasses robots.txt, a login, a CAPTCHA or an anti-bot response

It writes NOTHING to the database and changes no settings.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wfs import browser  # noqa: E402
from wfs import evidence_collection as ec  # noqa: E402
from wfs.collectors.chrono24 import Chrono24Collector  # noqa: E402
from wfs.collectors.ebay_sold import EbaySoldWebCollector  # noqa: E402
from wfs.collectors.watchcharts import WatchChartsCollector  # noqa: E402

LINE = "-" * 78


def _print_preflight(collector) -> dict:
    info = collector.preflight()
    print(f"  enabled        : {info.get('enabled')}")
    print(f"  transport      : {info.get('transport')}")
    if "robots_allowed" in info:
        print(f"  robots.txt     : {info.get('robots_url')}")
        print(f"  robots verdict : {info.get('robots_allowed')} for "
              f"{info.get('checked_url')}")
    print(f"  ready          : {info.get('ready')}")
    print(f"  reason         : {info.get('reason')}")
    return info


def _print_result(result) -> None:
    status = result.status
    print(f"  STATUS         : {status.status}")
    print(f"  records        : {status.record_count}")
    if status.error:
        print(f"  detail         : {status.error}")
    if status.detail:
        print(f"  note           : {status.detail}")
    for record in result.records[:5]:
        print(f"    - {record.evidence_type:<14} "
              f"£{record.price_gbp if record.price_gbp else '—':<9} "
              f"certainty={record.price_certainty:<22} "
              f"match={record.match_type}")
    if len(result.records) > 5:
        print(f"    … and {len(result.records) - 5} more")


def run(reference: str, brand: str, model: str, use_browser: bool) -> int:
    settings = ec.load_collection_settings()
    print(LINE)
    print("LIVE COLLECTOR SMOKE TEST")
    print(f"reference={reference}  brand={brand}  model={model}")
    bstat = browser.status()
    print(f"Playwright: installed={bstat['installed']} enabled={bstat['enabled']}")
    print(f"Master automatic evidence: {settings.master_enabled}")
    print(LINE)

    session = None
    if use_browser and bstat["usable"]:
        session = browser.BrowserSession().__enter__()
        print(f"Browser session available: {session.available}\n")

    # Sources that need no network are reported first, so you always see the
    # state of the routes that actually work before the ones that may not.
    from wfs.collectors.product_research import ProductResearchCollector
    from wfs.collectors.watchcharts_api import WatchChartsAPICollector
    from wfs import db as _db
    try:
        _conn = _db.connect()
        pr_info = ProductResearchCollector(_conn).preflight()
        print("\neBay Product Research (assisted, Level A)")
        print(f"  captured records: {pr_info.get('captured_records')}")
        print(f"  {pr_info.get('reason')}")
    except Exception as exc:
        print(f"\neBay Product Research — could not read local captures: {exc}")

    wc_info = WatchChartsAPICollector().preflight()
    print("\nWatchCharts API (Level B)")
    print(f"  enabled        : {wc_info.get('enabled')}")
    print(f"  key configured : {wc_info.get('key_configured')}")
    print(f"  ready          : {wc_info.get('ready')}")
    print(f"  reason         : {wc_info.get('reason')}")

    targets = [
        ("eBay Sold (public web)", ec.SRC_EBAY_SOLD_WEB,
         lambda: EbaySoldWebCollector(enabled=True, browser_session=session)),
        ("Chrono24 (active asking)", ec.SRC_CHRONO24,
         lambda: Chrono24Collector(enabled=True, browser_session=session)),
        ("WatchCharts (market context)", ec.SRC_WATCHCHARTS,
         lambda: WatchChartsCollector(enabled=True, browser_session=session)),
    ]

    working: list[str] = []
    try:
        for label, source_key, factory in targets:
            print(f"\n{label}")
            if not settings.is_enabled(source_key):
                print("  SKIPPED — not enabled in Settings → Automatic Market "
                      "Evidence. Enable it there first if you want to test it.")
                continue
            collector = factory()
            info = _print_preflight(collector)
            if not info.get("ready"):
                print("  → not attempting a fetch.")
                continue
            print("  fetching (throttled)…")
            result = collector.collect(reference, brand, model)
            _print_result(result)
            if result.records:
                working.append(label)
    finally:
        if session is not None:
            session.close()

    print("\n" + LINE)
    if working:
        print("Collectors returning real data: " + ", ".join(working))
    else:
        print("No external collector returned data. That is a legitimate result, "
              "not necessarily a bug — see the reason printed under each source.")
    print("Stored sold records and Local History are unaffected by this test.")
    print(LINE)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", default="79030N")
    parser.add_argument("--brand", default="Tudor")
    parser.add_argument("--model", default="Black Bay 58")
    parser.add_argument("--no-browser", action="store_true",
                        help="Skip the Playwright session even if available.")
    args = parser.parse_args()
    return run(args.reference, args.brand, args.model, not args.no_browser)


if __name__ == "__main__":
    raise SystemExit(main())

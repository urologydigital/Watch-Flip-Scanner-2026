"""Phase 5 sample output — MOCK DATA ONLY.

Run: python sample_output.py

Everything below is INVENTED for demonstration. The sold prices, sale counts and
listings are not real market data and must never be used to value a watch. The
purpose is to show what the engine produces for a BUY, a WATCH and a PASS.
"""
from __future__ import annotations

from wfs.flip_analysis import analyse_flip, dashboard_card, rank
from wfs.sold_market import COVERAGE_PARTIAL, SoldEvidence
from wfs.watchlist import WatchRef

BANNER = """
================================================================================
  WATCH FLIP SCANNER UK — PHASE 5 SAMPLE OUTPUT
  *** MOCK DATA — NOT REAL MARKET EVIDENCE ***
  Sale counts, sold prices and listings below are invented for demonstration.
================================================================================
"""


def mock_sold(reference: str, prices: list[float], period: int = 90) -> SoldEvidence:
    return SoldEvidence(reference, period, "MOCK_DATA", COVERAGE_PARTIAL,
                        exact_sale_count=len(prices),
                        family_sale_count=len(prices) + 5,
                        prices=prices,
                        notes="MOCK DATA — invented for demonstration.")


def build_cases():
    cases = []

    # --- 1. BUY: well-evidenced, liquid, full set, priced under MAX BUY -----
    cases.append(analyse_flip(
        listing={
            "item_id": "MOCK-BUY-1",
            "title": ("Tudor Black Bay 58 79030N 2023 full set box and papers, "
                      "recently serviced, all links, unpolished"),
            "price": 1850.0, "shipping": 0.0, "total_acquisition": 1850.0,
            "buying_format": "BUY_IT_NOW", "condition": "Pre-owned",
            "seller_feedback_pct": 99.9, "seller_feedback_score": 1450,
            "item_location": "Manchester, GB", "authenticity_guarantee": True,
            "returns_accepted": True, "image_url": "http://mock/img1",
            "brand": "TUDOR", "model": "Black Bay 58",
        },
        ref=WatchRef("TUDOR", "Black Bay 58", "79030N", 2600, 2750, 2900),
        sold_evidence=mock_sold("79030N", [2700, 2750, 2780, 2720, 2800, 2690,
                                           2760, 2740, 2770, 2710, 2790, 2730,
                                           2755, 2745]),
        active_listing_count=2,
    ))

    # --- 2. WATCH: good watch, priced slightly above MAX BUY ---------------
    cases.append(analyse_flip(
        listing={
            "item_id": "MOCK-WATCH-1",
            "title": ("Breitling Superocean Automatic 42 A17366 with box, "
                      "no papers, light scratches to bracelet"),
            "price": 1560.0, "shipping": 15.0, "total_acquisition": 1575.0,
            "buying_format": "BUY_IT_NOW", "condition": "Pre-owned",
            "seller_feedback_pct": 99.1, "seller_feedback_score": 320,
            "item_location": "Leeds, GB", "authenticity_guarantee": False,
            "returns_accepted": True, "image_url": "http://mock/img2",
            "brand": "BREITLING", "model": "SuperOcean Automatic 42",
        },
        ref=WatchRef("BREITLING", "SuperOcean Automatic 42", "A17366",
                     2150, 2300, 2450),
        sold_evidence=mock_sold("A17366", [2280, 2310, 2250, 2340, 2295, 2320,
                                           2270, 2305]),
        active_listing_count=5,
    ))

    # --- 3. PASS: huge apparent discount, but no sold evidence -------------
    cases.append(analyse_flip(
        listing={
            "item_id": "MOCK-PASS-1",
            "title": "Rado Captain Cook R32505203 automatic, sold as seen",
            "price": 620.0, "shipping": 0.0, "total_acquisition": 620.0,
            "buying_format": "AUCTION", "condition": "Used",
            "seller_feedback_pct": 96.2, "seller_feedback_score": 28,
            "item_location": "Warsaw, PL", "authenticity_guarantee": False,
            "returns_accepted": False, "image_url": None,
            "brand": "RADO", "model": "Captain Cook",
        },
        ref=WatchRef("RADO", "Captain Cook Automatic", "R32505203", 950, 1100, 1250),
        sold_evidence=mock_sold("R32505203", [1080]),
        active_listing_count=19,
    ))
    return cases


def main() -> None:
    print(BANNER)
    for a in rank(build_cases()):
        print(dashboard_card(a))
        print("\n" + "-" * 80 + "\n")

    print("Ranking check (default 'Best Flip Opportunities'):")
    for i, a in enumerate(rank(build_cases()), 1):
        print(f"  {i}. {a.verdict:<6} {a.ref.reference:<12} "
              f"Flip {a.flip_score.score:>5.1f}  "
              f"Confidence {a.confidence.score:>5.1f}  "
              f"Liquidity {a.liquidity.score:>5.1f}")
    print("\n*** END OF MOCK OUTPUT — none of the above is real market data. ***")


if __name__ == "__main__":
    main()

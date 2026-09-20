"""Watch Flip Scanner UK — Streamlit front end (Phase 2).

Run:  streamlit run app.py
"""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from wfs import (analytics, benchmarks, browser, cache, db, evidence_collection,
                 evidence_store, manual_evidence, observation, pipeline,
                 pipeline5, settings_store, theme)
from wfs.collectors import local_history
from wfs.collectors import product_research as pr
from wfs.collectors.watchcharts_api import WatchChartsAPICollector
from wfs.evidence_collection import (SOURCE_LABEL as EVIDENCE_SOURCE_LABEL,
                                     CollectionSettings,
                                     load_collection_settings,
                                     save_collection_settings,
                                     source_health_report)
from wfs.cross_check import CROSS_CHECK
from wfs.market_links import (chrono24_search_url, ebay_search_url, search_term,
                              watchcharts_search_url)
from wfs.economics import (CUSTOM, PROFILE_LABEL, UK_BUSINESS, UK_PRIVATE,
                           EconomicsEngine, get_profile)
from wfs.settings_store import EDITABLE_FIELDS, load_settings, save_settings
from wfs.config5 import CONFIG as P5
from wfs.flip_analysis import (SORT_MODES, allocate_capital, rank,
                               top_opportunities)
from wfs.ai import default_client
from wfs.asking_market import default_chrono24_provider, default_dealer_provider
# NOTE: legacy Phase 2 fee constants (SELLING_FEE_PCT, FIXED_SELLING_COSTS,
# REQUIRED_PROFIT_MARGIN) are deliberately NOT imported here. All seller
# economics come from the Phase 5.1 EconomicsEngine via ENGINE below.
from wfs.config import (AI_ENABLED, CHRONO24_ENABLED, OBSERVED_SALES_PATH,
                        SETTINGS, WATCHLIST_PATH)
from wfs.ebay import EbayClient
from wfs.sold_market import default_provider
from wfs.watchlist import brand_status, load_watchlist, set_brand_active

st.set_page_config(page_title="Watch Flip Scanner UK", layout="wide")
st.markdown(theme.CSS, unsafe_allow_html=True)

VERDICT_COLOUR = {"BUY": "🟢", CROSS_CHECK: "🔎", "WATCH": "🟡", "PASS": "⚪"}


@st.cache_resource
def get_conn():
    conn = db.connect()
    db.init_db(conn)
    return conn


conn = get_conn()

try:
    REFS = load_watchlist()
except FileNotFoundError as exc:
    st.error(str(exc))
    st.stop()

for r in REFS:
    db.upsert_reference(conn, r.as_dict())

USER_SETTINGS = load_settings()
ENGINE = settings_store.engine_from_settings(USER_SETTINGS)
P5_CONFIG = P5.with_overrides(economics=ENGINE)

manual_evidence.init_manual_schema(conn)
benchmarks.init_benchmark_schema(conn)
# Phase 5.3: additive migration. Creates the two new tables if absent and
# leaves every existing table, row and index untouched.
# Final 6.0 (spec s.20): back up before any migration, then prove nothing was
# lost. Backups live in db_backups/ and are never part of a release archive.
_ROWS_BEFORE = db.table_row_counts(conn)
_DB_BACKUP = None
if any(v > 0 for v in _ROWS_BEFORE.values()):
    try:
        _DB_BACKUP = db.backup_database()
    except Exception:
        _DB_BACKUP = None
NEW_TABLES = evidence_store.migrate(conn)
_ROWS_AFTER = db.table_row_counts(conn)
_ROWS_LOST = {t: (_ROWS_BEFORE[t], _ROWS_AFTER.get(t, 0))
              for t in _ROWS_BEFORE
              if _ROWS_AFTER.get(t, 0) < _ROWS_BEFORE[t]}
COLLECTION = load_collection_settings(USER_SETTINGS)
provider = default_provider()
chrono_provider = default_chrono24_provider()
dealer_provider = default_dealer_provider()
ai_client = default_client() if AI_ENABLED else None

st.title("WATCH FLIP SCANNER UK")
st.caption("Manual scan only. Profit is made at purchase, not at sale.")

_mode_cols = st.columns([1, 3])
_mode_cols[0].markdown(
    f"<div style='background:#E9E5F5;border-radius:8px;padding:8px 14px;"
    f"font-size:0.85rem'><strong>Economics: {ENGINE.mode_label}</strong></div>",
    unsafe_allow_html=True)
if ENGINE.profile.is_estimate:
    _mode_cols[1].caption("⚠️ This profile uses estimated fees. Replace them with "
                          "figures from a real invoice in Settings → Advanced Economics.")
else:
    _mode_cols[1].caption(
        f"Platform fees {ENGINE.profile.total_percentage_fees:.2%} · "
        f"seller costs £{ENGINE.profile.fixed_physical_costs:,.0f} per sale.")

if provider.name == "none":
    st.warning(
        "No sold-market evidence source is configured, so every market value falls "
        f"back to your seed figures and no time-to-sale can be estimated. Record "
        f"sales you observe in `{OBSERVED_SALES_PATH.name}` "
        "(see observed_sales.csv.example) or get eBay Marketplace Insights access. "
        "Until then, treat every verdict as WATCH at best.",
        icon="⚠️",
    )
else:
    st.info(f"Sold evidence source: **{provider.name}** — coverage is partial by nature.")

with st.expander("Evidence sources in use", expanded=False):
    st.write({
        "Sold evidence": provider.name,
        "Chrono24 (active asking)": chrono_provider.name,
        "Dealer / index context": dealer_provider.name,
        "AI analysis": getattr(ai_client, "model", "disabled"),
        "AI candidate cap per scan": SETTINGS.max_ai_candidates_per_scan,
    })
    if CHRONO24_ENABLED:
        st.warning(
            "Chrono24 automated lookup is ENABLED. It checks robots.txt and rate-limits "
            "itself, but automated collection may still breach Chrono24's terms of use. "
            "You have accepted that by setting WFS_CHRONO24_ENABLED=1.",
            icon="⚠️")
    st.caption("Chrono24 and dealer figures are ACTIVE ASKING data. They are never "
               "treated as sales and never used to derive sales volume.")

if not SETTINGS.ebay_configured:
    st.error("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not set. Copy .env.example to .env.")

(tab_flip, tab_evidence, tab_settings, tab_scan, tab_analytics, tab_watchlist,
 tab_history) = st.tabs(["Flip Intelligence", "Market Evidence", "Settings",
                         "Classic scan", "Analytics", "Watchlist settings",
                         "Scan history"])

with tab_flip:
    active_refs = [r for r in REFS if r.active]
    f1, f2, f3 = st.columns([1.1, 1, 2])
    run_flip = f1.button("RUN FLIP SCAN", type="primary",
                         disabled=not SETTINGS.ebay_configured, key="flipbtn")
    flip_ai = f2.checkbox("AI review", value=ai_client is not None,
                          disabled=ai_client is None, key="flipai")
    force_refresh = f3.checkbox(
        "Refresh evidence (ignore cache)", value=False, key="forcerefresh",
        help="Re-collect market evidence for the shortlist instead of reusing "
             "cached results.")
    f3.caption(f"{len(active_refs)} active references · Automatic evidence: "
               f"{'ON' if COLLECTION.master_enabled else 'OFF'} · "
               "Scheduled scan times 06:00, 12:00, 17:00, 21:00 UK.")

    if run_flip:
        bar = st.progress(0.0)
        status = st.empty()

        def fprogress(msg: str, frac: float) -> None:
            status.text(msg)
            bar.progress(frac)

        with st.spinner("Running flip intelligence…"):
            st.session_state["flip_result"] = pipeline5.run_flip_scan(
                conn, active_refs, client=EbayClient(),
                sold_provider=None, chrono24_provider=chrono_provider,
                ai_client=(ai_client if flip_ai else None),
                use_ai=flip_ai, progress=fprogress, config=P5_CONFIG,
                collection_settings=COLLECTION,
                force_refresh_evidence=force_refresh)

    fresult = st.session_state.get("flip_result")
    if not fresult:
        st.info("Run a flip scan to see opportunities ranked by genuine "
                "attractiveness rather than headline discount.")
    else:
        stats = fresult["stats"]
        m = st.columns(5)
        m[0].metric("Listings scanned", stats.listings_scanned)
        m[1].metric("Analysed", stats.analysed)
        m[2].metric("eBay API calls", stats.api_calls)
        m[3].metric("Calls avoided", stats.cache_hits + stats.queries_pruned)
        m[4].metric("AI calls", stats.ai_calls)
        st.caption(stats.summary_line())
        if getattr(stats, "evidence_references", 0):
            e = st.columns(4)
            e[0].metric("References collected", stats.evidence_references)
            e[1].metric("Evidence records", stats.evidence_records)
            e[2].metric("Evidence cache hits", stats.evidence_cache_hits)
            e[3].metric("Collector calls", stats.evidence_calls)

        unhealthy = [h for h in fresult.get("source_health", [])
                     if h["status"] in ("BLOCKED", "UNAVAILABLE")]
        if unhealthy:
            with st.expander(f"{len(unhealthy)} evidence source(s) unavailable — "
                             "scan completed regardless"):
                for h in unhealthy:
                    st.markdown(f"- **{h['label']}** — {h['status']}: "
                                f"{h['error'] or 'no detail'}")

        analyses = fresult["analyses"]
        verdicts = [a.verdict for a in analyses]
        v = st.columns(4)
        v[0].metric("🟢 BUY", verdicts.count("BUY"))
        v[1].metric("🔎 CROSS-CHECK", verdicts.count(CROSS_CHECK))
        v[2].metric("🟡 WATCH", verdicts.count("WATCH"))
        v[3].metric("⚪ PASS", verdicts.count("PASS"))

        if fresult["stats"].cross_check_suppressed:
            st.caption(f"{fresult['stats'].cross_check_suppressed} further listing(s) "
                       "met the cross-check bar but were not flagged, to keep the "
                       "manual shortlist workable. Raise WFS52_MAX_CROSS_CHECKS to see "
                       "more.")

        # --- TOP FLIP OPPORTUNITIES ------------------------------------
        # Ranked explicitly — BUY, then CROSS-CHECK, then WATCH, and within each
        # tier by Flip Score, confidence, velocity and net profit.
        top = top_opportunities(analyses, limit=30)
        if top:
            st.subheader("Top Flip Opportunities")
            st.caption("Ranked: actionable BUYs first, then cross-check candidates "
                       "worth verifying today, then WATCH. PASS listings excluded.")
            st.dataframe(
                pd.DataFrame([{
                    "": VERDICT_COLOUR.get(a.verdict, ""),
                    "Verdict": a.verdict,
                    "Brand": a.listing.get("brand"),
                    "Model": a.listing.get("model"),
                    "Reference": a.ref.reference,
                    "Price": a.listing.get("price"),
                    "Discount vs active": (f"{a.discount_pct:.1f}%"
                                           if a.discount_pct is not None else "—"),
                    "Discount vs benchmark": (
                        f"{a.discount_to_watchcharts_benchmark_pct:.1f}%"
                        if a.discount_to_watchcharts_benchmark_pct is not None
                        else "—"),
                    "MAX BUY": a.max_buy.standard,
                    "Net profit": a.net_profit,
                    "Confidence": a.confidence.score,
                    "Flip Score": a.flip_score.score,
                    "Link": a.listing_url,
                } for a in top[:30]]),
                hide_index=True, use_container_width=True,
                column_config={"Link": st.column_config.LinkColumn(
                    "Link", display_text="Open eBay")})

        c1, c2 = st.columns([2, 1])
        sort_mode = c1.selectbox("Rank by", list(SORT_MODES.keys()))
        show_pass = c2.checkbox("Include PASS", value=False)

        shown = rank(analyses, sort_mode)
        if not show_pass:
            shown = [a for a in shown if a.verdict != "PASS"]

        if not shown:
            st.success("NO SUITABLE FLIP OPPORTUNITIES FOUND.")
            st.caption("That is an acceptable and often correct result.")
        else:
            for a in shown[:40]:
                st.markdown(theme.render_card(a), unsafe_allow_html=True)
                with st.expander(f"Detail — {a.ref.reference}"):
                    d1, d2 = st.columns(2)
                    with d1:
                        st.markdown("**Market evidence**")
                        st.caption(a.evidence.describe())
                        st.json(a.evidence.as_dict(), expanded=False)
                        st.markdown("**Confidence reasoning**")
                        for r in a.confidence.reasons:
                            st.markdown(f"- {r}")
                    with d2:
                        st.markdown("**Flip Score components**")
                        st.json(a.flip_score.components, expanded=False)
                        st.caption(a.flip_score.formula)
                        st.markdown("**Risk factors**")
                        for r in a.risk.factors:
                            st.markdown(f"- {r}")
                        for r in a.risk.scrutiny_factors:
                            st.markdown(f"- ⚠️ {r}")
                        st.markdown("**Condition**")
                        for r in a.condition.positives:
                            st.markdown(f"- ✅ {r}")
                        for r in a.condition.negatives:
                            st.markdown(f"- ⚠️ {r}")
                    if a.ai_notes:
                        st.markdown("**AI review**")
                        for n in a.ai_notes:
                            st.markdown(f"- 🤖 {n}")
                    st.markdown("**Market research**")
                    if a.links:
                        cols = st.columns(len(a.links.buttons()))
                        for col, (label, url) in zip(cols, a.links.buttons()):
                            col.link_button(label, url, use_container_width=True)
                        st.caption(f"Search term used: `{a.links.term}` — brand and "
                                   "reference only, not the listing title.")

                    if a.benchmark and a.benchmark.is_usable:
                        st.markdown("**eBay UK active asking market** "
                                    "(not sold evidence)")
                        st.write(a.benchmark.as_dict())
                        if a.benchmark.excluded_count:
                            st.caption(
                                f"{a.benchmark.excluded_count} listing(s) excluded: "
                                + ", ".join(a.benchmark.exclusion_reasons))

                    if a.manual_benchmark is not None:
                        st.markdown("**Manual market benchmark — NOT confirmed "
                                    "sold evidence**")
                        mb_cols = st.columns(2)
                        mb_cols[0].metric(
                            "WatchCharts benchmark",
                            f"£{a.manual_benchmark.value:,.0f}")
                        if a.discount_to_watchcharts_benchmark_pct is not None:
                            mb_cols[1].metric(
                                "Discount to benchmark",
                                f"{a.discount_to_watchcharts_benchmark_pct:.1f}%")
                        st.caption(
                            f"{a.manual_benchmark.describe()} — Level "
                            f"{a.manual_benchmark.evidence_level} "
                            f"({a.manual_benchmark.evidence_kind}). This is market "
                            "context only: it cannot satisfy the sold-evidence gate "
                            "and cannot on its own produce a BUY.")
                        if a.benchmarks_agree is True:
                            st.success("Active eBay market and the manual benchmark "
                                       "broadly agree.")
                        elif a.benchmarks_agree is False:
                            st.warning("Market benchmarks disagree — verify manually.",
                                       icon="⚠️")
                        st.caption("The active eBay median and this benchmark are "
                                   "shown separately and are never averaged into a "
                                   "single 'market value'.")

                    agg = getattr(a, "evidence_aggregate", None)
                    if agg is not None and (agg.sold_records or agg.asking_records):
                        st.markdown("**Evidence records used**")
                        st.write(agg.as_dict())
                        rows = []
                        for rec in (agg.sold_records + agg.asking_records)[:40]:
                            rows.append({
                                "Type": rec.evidence_type,
                                "Source": rec.source,
                                "Price £": rec.price_gbp,
                                "Certainty": rec.price_certainty,
                                "Match": rec.match_type,
                                "Sale date": rec.sale_date,
                                "Counts for value": rec.usable_for_valuation,
                                "Counts for liquidity": rec.counts_for_liquidity,
                            })
                        if rows:
                            st.dataframe(pd.DataFrame(rows), hide_index=True,
                                         use_container_width=True)
                            st.caption("Best Offer sales count toward liquidity but "
                                       "contribute nothing to the valuation — the "
                                       "displayed price is not what was paid.")

                    audit = getattr(a, "comparable_audit", None)
                    if audit is not None and audit.decisions:
                        st.markdown("**Comparables used in this valuation**")
                        st.caption(audit.summary())
                        st.dataframe(pd.DataFrame([{
                            "Used": r["included"],
                            "Source": r["source"],
                            "Price £": r["price_gbp"],
                            "Date": r["sale_date"],
                            "Match": r["match_label"],
                            "Certainty": r["price_certainty"],
                            "Reason": r["reason"][:70],
                        } for r in audit.as_rows()]), hide_index=True,
                            use_container_width=True)

                    if a.decision.gates:
                        st.markdown("**Why not BUY**")
                        from wfs.decision import GATE_LABEL
                        for g in a.decision.gates:
                            st.markdown(f"- 🚫 {GATE_LABEL.get(g, g)}")

                    if a.cross_check:
                        st.markdown("**Cross-check assessment**")
                        st.caption(a.cross_check.summary)
                        for r in a.cross_check.reasons:
                            st.markdown(f"- ✅ {r}")
                        for r in a.cross_check.blockers:
                            st.markdown(f"- ⚠️ {r}")
                        for w in a.cross_check.warnings:
                            st.markdown(f"- 🔶 {w}")

        st.divider()
        st.subheader("Capital allocation")
        ac1, ac2 = st.columns([1, 2])
        budget = ac1.number_input("Available capital (£)", min_value=0.0,
                                  value=5000.0, step=250.0)
        include_watch = ac1.checkbox("Include WATCH candidates", value=False)
        plan = allocate_capital(analyses, budget, include_watch)
        ac2.write(plan.as_dict())
        if plan.selected:
            ac2.dataframe(pd.DataFrame([{
                "Reference": s.ref.reference,
                "Brand": s.listing.get("brand"),
                "Capital": s.acquisition,
                "Net profit": s.net_profit,
                "Profit/30d": s.base.profit_per_30d if s.base else None,
                "Verdict": s.verdict,
            } for s in plan.selected]), hide_index=True, use_container_width=True)
        st.caption(plan.note)



with tab_scan:
    c1, c2, c3, c4 = st.columns([1, 1, 1, 2])
    run_full = c1.button("RUN FULL SCAN", type="primary",
                         disabled=not SETTINGS.ebay_configured)
    run_ebay = c2.button("EBAY ONLY", disabled=not SETTINGS.ebay_configured)
    reanalyse = c3.button("REANALYSE SHORTLIST",
                          disabled="last_result" not in st.session_state)
    show_pass = c4.checkbox("Show ordinary PASS listings", value=False)
    use_ai = c4.checkbox("Run AI analysis on shortlist",
                         value=ai_client is not None, disabled=ai_client is None,
                         help="No AI key configured" if ai_client is None else None)

    if run_full or run_ebay or reanalyse:
        bar = st.progress(0.0)
        status = st.empty()

        def progress(msg: str, frac: float) -> None:
            status.text(msg)
            bar.progress(frac)

        with st.spinner("Scanning…"):
            result = pipeline.run_scan(
                conn, REFS, client=EbayClient(),
                mode="FULL" if run_full else "EBAY_ONLY",
                progress=progress, sold_provider=provider,
                chrono24_provider=(chrono_provider if run_full or reanalyse
                                   else None),
                dealer_provider=(dealer_provider if run_full or reanalyse else None),
                use_ai=use_ai,
            )
        st.session_state["last_result"] = result

    result = st.session_state.get("last_result")
    if result:
        st.subheader("Scan summary")
        s1, s2, s3, s4, s5 = st.columns(5)
        s1.metric("Listings scanned", result["listings_scanned"])
        s2.metric("Matched", result["matched"])
        s3.metric("Pre-filter candidates", result["candidates"])
        s4.metric("Deep analysed", len(result.get("analysed", [])))
        s5.metric("AI calls", result.get("ai_calls", 0))

        verdicts = [c.verdict for c in result.get("analysed", [])]
        v1, v2, v3 = st.columns(3)
        v1.metric("🟢 BUY", verdicts.count("BUY"))
        v2.metric("🟡 WATCH", verdicts.count("WATCH"))
        v3.metric("⚪ PASS", verdicts.count("PASS"))

        if result["errors"]:
            with st.expander(f"{len(result['errors'])} query error(s)"):
                for e in result["errors"]:
                    st.text(e)

        if result.get("delisted_since_last_scan"):
            st.caption(
                f"{result['delisted_since_last_scan']} previously tracked listing(s) "
                "no longer appear and have been marked delisted. Delisted does not "
                "mean sold.")
        if result.get("reappeared"):
            st.caption(f"{len(result['reappeared'])} listing(s) reappeared after "
                       "previously vanishing — often a relist at a new price.")

        st.caption(
            f"Pipeline: {result['listings_scanned']} scanned → "
            f"{result['candidates']} pre-filter candidates → "
            f"{len(result.get('analysed', []))} deep analysed → "
            f"{result.get('ai_calls', 0)} AI call(s)."
        )

        rows = pipeline.report_rows(result, include_pass=show_pass)
        if not rows:
            st.success("NO SUITABLE FLIP CANDIDATES FOUND.")
            st.caption("That is an acceptable and often correct result.")
        else:
            display = pd.DataFrame(
                [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
            )
            display.insert(0, "", [VERDICT_COLOUR.get(r["Verdict"], "") for r in rows])
            st.dataframe(
                display, use_container_width=True, hide_index=True,
                column_config={"Link": st.column_config.LinkColumn("Link")},
            )

            # --- candidate detail (spec s.17) ------------------------------
            st.subheader("Candidate detail")
            by_id = {c.item_id: c for c in result["analysed"]}
            labels = [f"{VERDICT_COLOUR.get(r['Verdict'],'')} {r['Brand']} "
                      f"{r['Reference']} — £{(r['Acquisition'] or 0):,.0f}" for r in rows]
            idx = st.selectbox("Select a candidate", range(len(rows)),
                               format_func=lambda i: labels[i])
            cand = by_id[rows[idx]["_item_id"]]
            l = cand.listing

            st.markdown(f"### {VERDICT_COLOUR.get(cand.verdict,'')} {cand.verdict} — "
                        f"{l.get('brand')} {l.get('model')} {cand.ref.reference}")
            if cand.verdict != cand.deterministic_verdict:
                st.caption(f"Deterministic verdict was {cand.deterministic_verdict}.")
            for reason in cand.verdict_reasons:
                st.markdown(f"- {reason}")
            for note in cand.ai_notes:
                st.markdown(f"- 🤖 {note}")

            d1, d2 = st.columns([1, 2])
            with d1:
                if l.get("image_url"):
                    st.image(l["image_url"], use_container_width=True)
                st.markdown("**LISTING**")
                st.write({
                    "Price": l.get("price"),
                    "Shipping": l.get("shipping"),
                    "Total acquisition": cand.acquisition,
                    "Format": l.get("buying_format"),
                    "Best offer": l.get("best_offer"),
                    "Condition": l.get("condition"),
                    "Seller": l.get("seller"),
                    "Feedback %": l.get("seller_feedback_pct"),
                    "Location": l.get("item_location"),
                    "Authenticity Guarantee": l.get("authenticity_guarantee"),
                })
                if l.get("url"):
                    st.link_button("Open on eBay", l["url"])

            with d2:
                st.markdown("**MAX BUY**")
                mb = cand.maxbuy
                m1, m2 = st.columns(2)
                m1.metric("MAX BUY", f"£{mb.value:,.0f}" if mb.value else "n/a")
                m2.metric("Headroom",
                          f"£{mb.headroom:,.0f}" if mb.headroom is not None else "n/a")
                st.caption(mb.explanation)

                st.markdown("**PRICING — three resale routes**")
                sc = cand.scenarios
                if sc:
                    st.dataframe(pd.DataFrame([{
                        "Route": s.name,
                        "Sale price": f"£{s.price:,.0f}",
                        "Net after fees": f"£{s.net_proceeds:,.0f}",
                        "Profit": (f"£{s.gross_profit:,.0f}"
                                   if s.gross_profit is not None else "n/a"),
                        "Est. time to sale": (f"{s.days_low}–{s.days_high} days"
                                              if s.days_low else "cannot be estimated"),
                    } for s in sc.values()]), hide_index=True,
                        use_container_width=True)
                    st.caption("Estimates based on observed UK sales frequency and "
                               "current competition. Never guarantees.")

                st.markdown("**MARKET**")
                mv = cand.market
                st.write({
                    "Market low": mv.market_low,
                    "Market mid": mv.market_mid,
                    "Market high": mv.market_high,
                    "Confidence": mv.confidence,
                    "Reason": mv.confidence_reason,
                    "Sources": ", ".join(mv.sources),
                    "Weights": mv.weights,
                })

                st.markdown("**SOLD EVIDENCE**")
                st.caption(cand.sold.describe())

                st.markdown("**ACTIVE ASKING CONTEXT**")
                for evidence in (cand.chrono24, cand.dealer):
                    if evidence is not None:
                        st.caption(evidence.describe())
                        if evidence.has_evidence:
                            st.write({
                                "Listings observed": evidence.listings_observed,
                                "Dealer": evidence.dealer_count,
                                "Private": evidence.private_count,
                                "Countries": ", ".join(evidence.countries) or "n/a",
                            })
                if cand.chrono24 is None and cand.dealer is None:
                    st.caption("Not checked on this scan.")

                st.markdown("**LIQUIDITY**")
                liq = cand.liquidity
                st.write({
                    "Rating": liq.rating,
                    "Observed exact-reference sales": liq.exact_sales,
                    "Observed model-family sales": liq.family_sales,
                    "Period (days)": liq.period_days,
                    "Competing UK listings observed": liq.competing_listings,
                    "Median observed sale price": liq.median_sale_price,
                    "Price dispersion": liq.price_dispersion,
                    "Monthly sale rate": liq.monthly_sale_rate,
                })
                st.caption(liq.basis)

                st.markdown(f"**RISK — {cand.risk.level}** "
                            f"(structural buffer {cand.risk.buffer:.0%}, "
                            f"scrutiny {cand.risk.scrutiny_buffer:.0%})")
                for f in cand.risk.factors:
                    st.markdown(f"- {f}")
                for f in cand.risk.scrutiny_factors:
                    st.markdown(f"- ⚠️ {f}")
                if not cand.risk.factors and not cand.risk.scrutiny_factors:
                    st.markdown("- No risk flags raised from available fields.")

                if cand.ai is not None:
                    st.markdown("**AI ANALYSIS**")
                    if not cand.ai.ok:
                        st.caption(f"Unavailable: {cand.ai.error}")
                    else:
                        a = cand.ai.payload
                        st.write({
                            "Reference verified": a.get("reference_verified"),
                            "Likely reference": a.get("likely_reference"),
                            "Authenticity risk": a.get("authenticity_risk"),
                            "Condition risk": a.get("condition_risk"),
                            "Document risk": a.get("document_risk"),
                            "AI verdict": a.get("verdict"),
                            "AI confidence": a.get("confidence"),
                        })
                        st.markdown("**WHY THIS MAY BE MISPRICED**")
                        st.write(a.get("mispricing_reason"))
                        st.caption(a.get("pricing_comment", ""))
                        st.caption(a.get("liquidity_comment", ""))
                        for w in a.get("warnings", []):
                            st.markdown(f"- ⚠️ {w}")
                        st.caption("The AI cannot change MAX BUY and cannot upgrade "
                                   "a verdict — only make it more cautious.")

                hist = db.price_history(conn, cand.item_id)
                if len(hist) > 1:
                    st.markdown("**PRICE HISTORY**")
                    st.dataframe(
                        pd.DataFrame([dict(h) for h in hist])[["observed_at", "price"]],
                        hide_index=True, use_container_width=True)

with tab_analytics:
    summary = analytics.database_summary(conn)
    if not summary["scans_run"]:
        st.info("No completed scans yet. Analytics build up as you run scans over time.")
    else:
        a1, a2, a3, a4 = st.columns(4)
        a1.metric("Scans run", summary["scans_run"])
        a2.metric("Listings tracked", summary["listings_tracked"])
        a3.metric("Still active", summary["listings_active"])
        a4.metric("Delisted", summary["listings_delisted"])
        if summary["tracking_since"]:
            st.caption(f"Tracking since {summary['tracking_since'][:10]}. "
                       "Delisted means the listing vanished — it may have sold, been "
                       "ended, or expired. None of it is treated as sold evidence.")

        st.subheader("Live price drops")
        drops = analytics.active_price_drops(conn, min_pct=3.0)
        if not drops:
            st.caption("No active listing has fallen more than 3% since first seen.")
        else:
            st.dataframe(pd.DataFrame(drops), use_container_width=True,
                         hide_index=True,
                         column_config={"Link": st.column_config.LinkColumn("Link")})

        st.subheader("Reference performance")
        stats = [s.as_dict() for s in analytics.reference_performance(conn)]
        if not stats:
            st.caption("Nothing tracked yet.")
        else:
            st.dataframe(pd.DataFrame(stats), use_container_width=True,
                         hide_index=True)
            st.caption("Median days to delist is a weak proxy for time-to-sale. "
                       "Blank statistics mean too few observations to report.")

        st.subheader("Asking-price drift")
        all_refs = sorted({s["reference"] for s in stats}) if stats else []
        pick = st.selectbox("Reference", ["ALL"] + all_refs)
        drift = analytics.price_drift(conn, None if pick == "ALL" else pick)
        col_a, col_b = st.columns(2)
        col_a.markdown("**Delisted listings**")
        col_a.write(drift["delisted"])
        col_b.markdown("**Still active**")
        col_b.write(drift["still_active"])
        st.caption(drift["caveat"])

        st.subheader("Listings that keep coming back")
        persistent = analytics.persistent_listings(conn, min_scans=3)
        if not persistent:
            st.caption("No listing has appeared in three or more scans yet.")
        else:
            st.dataframe(pd.DataFrame(persistent), use_container_width=True,
                         hide_index=True,
                         column_config={"Link": st.column_config.LinkColumn("Link")})
            st.caption("A watch sitting through several scans is usually priced above "
                       "what the market will pay, or the reference is thinner than "
                       "asking prices suggest.")

        st.subheader("Scan trends")
        trends = analytics.scan_trends(conn)
        if len(trends) > 1:
            tdf = pd.DataFrame(trends).set_index("id")
            st.line_chart(tdf[["listings_scanned", "prefilter_candidates",
                               "deep_analysed"]])
            st.line_chart(tdf[["candidate_rate_pct"]])
        else:
            st.caption("Trends appear once you have run more than one scan.")


with tab_evidence:
    st.subheader("Automatic Market Evidence")
    auto_summary = evidence_store.evidence_summary(conn)
    a1, a2, a3 = st.columns(3)
    a1.metric("Collected records", auto_summary["records"])
    a2.metric("References covered", auto_summary["references"])
    a3.metric("Last updated", (auto_summary["last_updated"] or "—")[:16])
    if auto_summary["by_type"]:
        st.caption("By evidence type: " + ", ".join(
            f"{k} {v}" for k, v in sorted(auto_summary["by_type"].items())))

    st.markdown("**Source health**")
    health_rows = source_health_report(conn)
    st.dataframe(pd.DataFrame([{
        "Source": h["label"],
        "Status": h["status"],
        "Records": h["record_count"],
        "Last success": (h["last_success"] or "—")[:16],
        "Cache": h["cache_status"] or "—",
        "Detail": (h["error"] or h["detail"] or "")[:80],
    } for h in health_rows]), hide_index=True, use_container_width=True)
    st.caption("A source showing BLOCKED, UNAVAILABLE or ERROR never stops a "
               "scan — the remaining sources carry on.")

    with st.expander("Diagnose sources (no data is collected)"):
        st.caption("Checks each collector's transport and robots.txt verdict "
                   "without fetching any evidence.")
        if st.button("Run diagnostics", key="evidence_preflight_btn"):
            try:
                rows = evidence_collection.CollectorRegistry(
                    conn, COLLECTION).preflight()
                st.dataframe(pd.DataFrame([{
                    "Source": evidence_collection.SOURCE_LABEL.get(
                        r.get("source"), r.get("source")),
                    "Enabled": r.get("enabled"),
                    "Transport": r.get("transport"),
                    "robots.txt": r.get("robots_allowed"),
                    "Ready": r.get("ready"),
                    "Reason": r.get("reason", "")[:140],
                } for r in rows]), hide_index=True, use_container_width=True)
            except Exception as exc:
                st.error(f"Diagnostics failed: {type(exc).__name__}: {exc}")
        st.caption("For a full live check against one reference, run "
                   "`python tools/collector_smoke_test.py` in a terminal.")

    with st.expander("Browse collected evidence"):
        all_refs_ev = sorted({r.reference for r in REFS})
        ev_ref = st.selectbox("Reference", all_refs_ev, key="evref")
        ev_type = st.selectbox("Evidence type",
                               ["All", "SOLD", "ACTIVE_ASKING",
                                "MARKET_CONTEXT", "OBSERVATION"], key="evtype")
        records = evidence_store.load_evidence(
            conn, ev_ref, None if ev_type == "All" else ev_type)
        if not records:
            st.info("No collected evidence for this reference yet.")
        else:
            st.dataframe(pd.DataFrame([{
                "Type": r.evidence_type, "Source": r.source,
                "Price £": r.price_gbp, "Certainty": r.price_certainty,
                "Match": r.match_type, "Sale date": r.sale_date,
                "Retrieved": (r.retrieved_at or "")[:16],
                "Value?": r.usable_for_valuation,
                "Liquidity?": r.counts_for_liquidity,
            } for r in records]), hide_index=True, use_container_width=True)

        st.markdown("**Local UK market history**")
        hist = local_history.market_history(conn, ev_ref)
        st.caption(hist.describe())
        if hist.histories:
            st.dataframe(pd.DataFrame([{
                "Status": h.status_label,
                "First seen": (h.first_seen or "")[:10],
                "Days on market": h.days_on_market,
                "Initial £": h.initial_price,
                "Latest £": h.latest_price,
                "Lowest £": h.lowest_price_seen,
                "Price changes": h.price_change_count,
                "Drop %": h.total_drop_pct,
                "Confirmed by": h.confirmed_by or "—",
            } for h in hist.histories]), hide_index=True,
                use_container_width=True)

    st.divider()
    st.subheader("Market Evidence Import")
    st.caption("Manually recorded sales are the strongest evidence most users can "
               "obtain. Importing is optional — the scanner runs without it.")

    summary = manual_evidence.evidence_summary(conn)
    e1, e2, e3 = st.columns(3)
    e1.metric("Sold records stored", summary["records"])
    e2.metric("References covered", summary["references"])
    e3.metric("Most recent sale", summary["latest"] or "—")

    st.download_button("Download CSV template", manual_evidence.TEMPLATE_CSV,
                       file_name="manual_sold_evidence_template.csv",
                       mime="text/csv")

    uploaded = st.file_uploader("Upload sold evidence CSV", type=["csv"])
    if uploaded is not None and st.button("Import records", type="primary"):
        text = uploaded.getvalue().decode("utf-8", errors="replace")
        report = manual_evidence.import_csv_text(conn, text)
        st.session_state["import_report"] = report.as_dict()

    report = st.session_state.get("import_report")
    if report:
        r1, r2, r3 = st.columns(3)
        r1.metric("Imported", report["imported"])
        r2.metric("Duplicates rejected", report["duplicates_rejected"])
        r3.metric("Invalid rows", report["invalid"])
        if report["references_affected"]:
            st.success("References affected: "
                       + ", ".join(report["references_affected"]))
        if report["errors"]:
            with st.expander(f"{len(report['errors'])} row(s) not imported"):
                st.dataframe(pd.DataFrame(report["errors"]), hide_index=True,
                             use_container_width=True)

    st.divider()
    st.subheader("Stored sold records")
    records = manual_evidence.stored_records(conn)
    if not records:
        st.info("No manually recorded sales yet.")
    else:
        st.dataframe(pd.DataFrame(records)[
            ["brand", "reference", "model", "sold_price_gbp", "sold_date",
             "condition", "full_set", "source"]],
            hide_index=True, use_container_width=True)

    st.divider()
    st.subheader("eBay Product Research — confirmed sold evidence (Level A)")
    st.caption("The only reliable route to Level A evidence for most accounts. "
               "This prepares the exact search; you open it in your own "
               "signed-in browser and paste the results back. The app never "
               "touches your login, cookies or password.")

    pr_refs = sorted({r.reference for r in REFS})
    pr_ref = st.selectbox("Reference", pr_refs, key="pr_ref")
    pr_entry = next((r for r in REFS if r.reference == pr_ref), None)
    pr_brand = pr_entry.brand if pr_entry else ""
    pr_model = pr_entry.model if pr_entry else ""

    pr_summary = pr.capture_summary(conn, pr_ref)
    pc1, pc2 = st.columns(2)
    pc1.metric("Captured records (this reference)", pr_summary["records"])
    pc2.metric("Last capture", (pr_summary["last_capture"] or "—")[:16])

    st.link_button("Open eBay Product Research for this reference",
                   pr.research_url(pr_brand, pr_ref, pr_model),
                   use_container_width=True)
    with st.expander("How to capture (30 seconds)"):
        for i, step in enumerate(pr.instructions(pr_brand, pr_ref), 1):
            st.markdown(f"{i}. {step}")

    pr_text = st.text_area(
        "Paste the sold results here (include the header row)",
        key="pr_paste", height=150,
        placeholder="Title\tSold Price\tDate Sold\tCondition\tBest Offer")
    if st.button("Import Product Research data", type="primary",
                 key="pr_import_btn", disabled=not pr_text.strip()):
        report = pr.import_capture(conn, pr_text, pr_ref, pr_brand, pr_model)
        st.session_state["pr_report"] = {
            "summary": report.summary(),
            "rows": [r.as_dict() for r in report.rows]}

    pr_report = st.session_state.get("pr_report")
    if pr_report:
        st.success(pr_report["summary"])
        st.dataframe(pd.DataFrame([{
            "Accepted": r["accepted"],
            "Title": (r["title"] or "")[:60],
            "Sold £": r["sold_price_gbp"],
            "Date": r["sale_date"],
            "Match": r["match_type"],
            "Certainty": r["price_certainty"],
            "Reason": r["reason"][:70],
        } for r in pr_report["rows"]]), hide_index=True,
            use_container_width=True)
        st.caption("Rows that do not carry the exact reference are rejected "
                   "rather than guessed at. Best Offer sales are kept for "
                   "liquidity but contribute no price.")

    st.divider()
    st.subheader("WatchCharts API (Level B, optional)")
    _wc = WatchChartsAPICollector()
    _wc_pre = _wc.preflight()
    st.write({"enabled": _wc_pre["enabled"],
              "key configured": _wc_pre["key_configured"],
              "ready": _wc_pre["ready"],
              "status": _wc_pre["reason"]})
    st.caption("Configure with WFS_WATCHCHARTS_API_ENABLED and "
               "WFS_WATCHCHARTS_API_KEY in .env. The scanner works fully "
               "without it — WatchCharts is valuation context, never a sale.")

    st.divider()
    st.subheader("Manual market benchmark")
    st.caption("No paid API required. Click through to WatchCharts or Chrono24, "
               "read the figure yourself, and record it here. The evidence type is "
               "derived from the source — a Chrono24 asking price can never be "
               "stored as a sale.")

    bsummary = benchmarks.benchmark_summary(conn)
    b1, b2 = st.columns(2)
    b1.metric("Benchmarks stored", bsummary["records"])
    b2.metric("References covered", bsummary["references"])

    all_refs = sorted({r.reference for r in REFS})
    bc1, bc2 = st.columns([2, 1])
    bench_ref = bc1.selectbox("Reference", all_refs, key="benchref")
    bench_brand = next((r.brand for r in REFS if r.reference == bench_ref), "")

    lc = st.columns(3)
    lc[0].link_button("Check WatchCharts",
                      watchcharts_search_url(bench_brand, bench_ref),
                      use_container_width=True)
    lc[1].link_button("Chrono24 Comparables",
                      chrono24_search_url(bench_brand, bench_ref),
                      use_container_width=True)
    lc[2].link_button("eBay Sold/Completed",
                      ebay_search_url(bench_brand, bench_ref, sold=True),
                      use_container_width=True)
    st.caption(f"Search term: `{search_term(bench_brand, bench_ref)}`")

    f1, f2, f3 = st.columns(3)
    bench_source = f1.selectbox(
        "Source", [benchmarks.MANUAL_WATCHCHARTS, benchmarks.MANUAL_CHRONO24,
                   benchmarks.MANUAL_EBAY_SOLD, benchmarks.OTHER_CONFIRMED],
        format_func=lambda x: benchmarks.SOURCE_LABEL[x])
    bench_value = f2.number_input("Value (£)", min_value=0.0, value=0.0, step=25.0)
    bench_sample = f3.number_input("Comparables seen (optional)", min_value=0,
                                   value=0, step=1)
    g1, g2 = st.columns(2)
    bench_low = g1.number_input("Low (£, optional)", min_value=0.0, value=0.0, step=25.0)
    bench_high = g2.number_input("High (£, optional)", min_value=0.0, value=0.0, step=25.0)
    bench_notes = st.text_input("Notes (optional)")

    derived_kind = benchmarks.kind_for_source(bench_source)
    if derived_kind == benchmarks.KIND_SOLD:
        st.success(f"Will be stored as **{derived_kind}** — counts as sold evidence.")
    else:
        st.info(f"Will be stored as **{derived_kind}** — market context only. "
                "This cannot on its own produce a BUY.")

    if st.button("Record benchmark", type="primary", disabled=bench_value <= 0):
        try:
            saved = benchmarks.record_benchmark(
                conn, bench_ref, bench_source, bench_value,
                low_value=bench_low or None, high_value=bench_high or None,
                sample_size=bench_sample or None, brand=bench_brand,
                notes=bench_notes or None)
            st.success(f"Recorded — {saved.describe()}")
        except ValueError as exc:
            st.error(str(exc))

    stored_b = benchmarks.all_benchmarks(conn)
    if stored_b:
        st.dataframe(pd.DataFrame(stored_b)[
            ["reference", "source", "evidence_kind", "value", "currency",
             "sample_size", "recorded_at", "notes"]],
            hide_index=True, use_container_width=True)

    st.divider()
    st.subheader("Scanner-observed activity")
    st.caption("Built from listings this scanner has watched. A disappearance is "
               "NOT a confirmed sale — outcomes are classified conservatively.")
    refs_with_history = sorted({r.reference for r in REFS})
    pick_ref = st.selectbox("Reference", refs_with_history, key="obsref")
    activity = observation.observed_activity(conn, pick_ref)
    st.caption(activity.describe())
    if activity.observations:
        st.dataframe(pd.DataFrame([{
            "Status": observation.CLASSIFICATION_LABEL.get(o.status, o.status),
            "First seen": (o.first_seen_at or "")[:10],
            "Last seen": (o.last_seen_at or "")[:10],
            "Days tracked": o.active_duration_days,
            "Scans": o.times_seen,
            "First price": o.first_price,
            "Last price": o.last_price,
            "Drops": o.price_drops,
            "Basis": o.rationale,
        } for o in activity.observations]), hide_index=True,
            use_container_width=True)


with tab_settings:
    st.subheader("Seller economics")
    st.caption("These assumptions drive every net profit, ROI, MAX BUY and "
               "recommendation in the application. Credentials are never stored "
               "here — they stay in .env.")

    profile_names = [UK_PRIVATE, UK_BUSINESS, CUSTOM]
    current = USER_SETTINGS.get("seller_profile", UK_PRIVATE)
    chosen = st.radio("Seller Profile", profile_names,
                      index=profile_names.index(current)
                      if current in profile_names else 0,
                      format_func=lambda n: PROFILE_LABEL[n], horizontal=True)

    preview = get_profile(chosen, **(USER_SETTINGS.get("overrides") or {}))
    st.info(preview.notes)
    if preview.is_estimate:
        st.warning("This profile is an ESTIMATE. Replace the fee values below with "
                   "figures from a real eBay invoice before trusting the output.",
                   icon="⚠️")

    overrides = dict(USER_SETTINGS.get("overrides") or {})

    with st.expander("Advanced Economics", expanded=(chosen == CUSTOM)):
        st.markdown("**Platform fees**")
        c1, c2 = st.columns(2)
        overrides["transaction_fee_pct"] = c1.number_input(
            "Transaction fee (%)", 0.0, 50.0,
            float(preview.transaction_fee_pct * 100), 0.1) / 100
        overrides["payment_processing_fee_pct"] = c2.number_input(
            "Payment processing fee (%)", 0.0, 20.0,
            float(preview.payment_processing_fee_pct * 100), 0.1) / 100
        overrides["regulatory_operating_fee_pct"] = c1.number_input(
            "Regulatory operating fee (%)", 0.0, 10.0,
            float(preview.regulatory_operating_fee_pct * 100), 0.05) / 100
        overrides["fixed_fee_gbp"] = c2.number_input(
            "Fixed per-order fee (£)", 0.0, 50.0, float(preview.fixed_fee_gbp), 0.05)

        st.markdown("**Optional charges**")
        c3, c4 = st.columns(2)
        overrides["promoted_listing_enabled"] = c3.checkbox(
            "Promoted listing", value=bool(preview.promoted_listing_enabled))
        overrides["promoted_listing_fee_pct"] = c3.number_input(
            "Promoted listing fee (%)", 0.0, 30.0,
            float(preview.promoted_listing_fee_pct * 100), 0.1) / 100
        overrides["international_sale"] = c4.checkbox(
            "Assume international sale", value=bool(preview.international_sale))
        overrides["international_fee_pct"] = c4.number_input(
            "International fee (%)", 0.0, 20.0,
            float(preview.international_fee_pct * 100), 0.1) / 100

        st.markdown("**Seller-side costs**")
        c5, c6, c7 = st.columns(3)
        overrides["shipping_cost_gbp"] = c5.number_input(
            "Postage (£)", 0.0, 200.0, float(preview.shipping_cost_gbp), 1.0)
        overrides["insurance_cost_gbp"] = c6.number_input(
            "Insurance (£)", 0.0, 200.0, float(preview.insurance_cost_gbp), 1.0)
        overrides["packaging_cost_gbp"] = c7.number_input(
            "Packaging (£)", 0.0, 100.0, float(preview.packaging_cost_gbp), 1.0)
        overrides["authentication_cost_gbp"] = c5.number_input(
            "Authentication (£)", 0.0, 200.0,
            float(preview.authentication_cost_gbp), 1.0)
        overrides["miscellaneous_cost_gbp"] = c6.number_input(
            "Miscellaneous (£)", 0.0, 500.0,
            float(preview.miscellaneous_cost_gbp), 1.0)

        st.markdown("**Reserves and allowances**")
        c8, c9 = st.columns(2)
        overrides["service_reserve_pct"] = c8.number_input(
            "Service reserve (% of resale, applied only when condition warrants)",
            0.0, 25.0, float(preview.service_reserve_pct * 100), 0.5) / 100
        overrides["negotiation_allowance_pct"] = c9.number_input(
            "Negotiation allowance (% off asking when buying)",
            0.0, 40.0, float(preview.negotiation_allowance_pct * 100), 0.5) / 100

    if st.button("Save settings", type="primary"):
        save_settings({"seller_profile": chosen, "overrides": overrides})
        st.success("Saved. Reload the page to apply.")
        st.rerun()

    st.divider()
    st.subheader("Automatic Market Evidence")
    st.caption("Sources that need no network or permission run automatically. "
               "Public-web collectors are optional and off unless you enable "
               "them here; your choice is remembered.")

    # --- widget state ------------------------------------------------------
    # Every widget below carries an explicit, stable key. Without one, Streamlit
    # derives a widget's identity from its label, parameters AND its position in
    # the element tree — so a conditionally rendered element (the public-web
    # warning) inserted between the checkboxes and the Save button silently
    # changed the button's identity and left it inert. Explicit keys make
    # identity independent of both value and position.
    EVIDENCE_SOURCE_WIDGETS = [
        (evidence_collection.SRC_LOCAL_HISTORY, "Local History", True,
         "Built from your own scans. No external access."),
        (evidence_collection.SRC_EBAY_SOLD_STORED, "Stored sold records", True, None),
        (evidence_collection.SRC_EBAY_SOLD_INSIGHTS,
         "eBay Marketplace Insights (licensed API)", True,
         "Only operates if eBay has granted your keyset access."),
        (evidence_collection.SRC_CHRONO24, "Chrono24 (active asking)", False, None),
        (evidence_collection.SRC_WATCHCHARTS, "WatchCharts (market context)",
         False, None),
        (evidence_collection.SRC_EBAY_SOLD_WEB, "eBay Sold (public web)", False, None),
    ]
    PUBLIC_WEB_SOURCES = (evidence_collection.SRC_CHRONO24,
                          evidence_collection.SRC_WATCHCHARTS,
                          evidence_collection.SRC_EBAY_SOLD_WEB)

    def _ev_key(source: str) -> str:
        return f"evsrc_{source}"

    # Seed session state once from the persisted settings. After that the
    # widgets own their own state, so a rerun never reverts the user's ticks.
    if "evidence_state_loaded" not in st.session_state:
        st.session_state["evidence_master"] = COLLECTION.master_enabled
        st.session_state["evidence_cap"] = int(COLLECTION.shortlist_cap)
        st.session_state["evidence_lookback"] = int(COLLECTION.lookback_days)
        for source, _label, default, _help in EVIDENCE_SOURCE_WIDGETS:
            st.session_state[_ev_key(source)] = bool(
                COLLECTION.sources.get(source, default))
        st.session_state["evidence_state_loaded"] = True

    st.toggle("AUTOMATIC MARKET EVIDENCE", key="evidence_master",
              help="Master switch for all automatic collection.")

    sc1, sc2 = st.columns(2)
    with sc1:
        st.markdown("**Always safe** (no network or permission needed)")
        for source, label, _default, helptext in EVIDENCE_SOURCE_WIDGETS[:3]:
            st.checkbox(label, key=_ev_key(source), help=helptext)
    with sc2:
        st.markdown("**Optional public web** — your responsibility")
        for source, label, _default, helptext in EVIDENCE_SOURCE_WIDGETS[3:]:
            st.checkbox(label, key=_ev_key(source), help=helptext)
        # Fixed slot: the warning is written into a placeholder that always
        # exists, so showing or hiding it never shifts any sibling widget.
        public_web_warning = st.empty()

    cap_col, look_col = st.columns(2)
    cap_col.number_input("References per scan (collection cap)", 1, 100,
                         key="evidence_cap")
    look_col.number_input("Evidence lookback (days)", 30, 1095, step=30,
                          key="evidence_lookback")

    bstat = browser.status()
    st.caption(
        f"Playwright: {'installed' if bstat['installed'] else 'not installed'}"
        f" · {'enabled' if bstat['enabled'] else 'disabled'}"
        + (f" · install with `{bstat['install_hint']}`"
           if bstat["install_hint"] else "")
        + ". Optional — the scanner runs fully without it.")

    # --- read current widget state ----------------------------------------
    def _pending_collection_settings() -> CollectionSettings:
        """Build a CollectionSettings from live widget state.

        Starts from the persisted source map so that sources WITHOUT a checkbox
        (eBay Active, which is always on and reuses scan data) are preserved.
        Rebuilding from the widget rows alone dropped those keys, which both
        corrupted the saved file and made the saved/unsaved comparison
        permanently unequal.
        """
        sources = dict(COLLECTION.sources)
        for source, _label, default, _help in EVIDENCE_SOURCE_WIDGETS:
            sources[source] = bool(st.session_state.get(_ev_key(source), default))
        return CollectionSettings(
            master_enabled=bool(st.session_state.get("evidence_master", True)),
            sources=sources,
            shortlist_cap=int(st.session_state.get("evidence_cap",
                                                   COLLECTION.shortlist_cap)),
            lookback_days=int(st.session_state.get("evidence_lookback",
                                                   COLLECTION.lookback_days)))

    def _save_evidence_settings() -> None:
        """Persist on click, BEFORE the script reruns.

        An on_click callback is the correct place for this. Doing the save in the
        body and then calling st.rerun() discarded the run's output, so the
        confirmation never reached the browser and the page still showed the
        pre-save state.
        """
        try:
            pending_now = _pending_collection_settings()
            save_collection_settings(pending_now, load_settings())
            st.session_state["evidence_saved_summary"] = [
                evidence_collection.SOURCE_LABEL.get(k, k)
                for k, v in pending_now.sources.items() if v]
            st.session_state["evidence_save_error"] = None
        except Exception as exc:   # surface it rather than failing silently
            st.session_state["evidence_save_error"] = f"{type(exc).__name__}: {exc}"

    pending = _pending_collection_settings()
    has_changes = pending.as_dict() != COLLECTION.as_dict()

    if any(pending.sources.get(k) for k in PUBLIC_WEB_SOURCES):
        public_web_warning.warning(
            "Public-web collection checks robots.txt and throttles itself, but "
            "each site's terms of use still apply and enabling this is your "
            "decision.", icon="⚠️")

    # The Save button is never disabled, and the write happens in its callback.
    save_col, status_col = st.columns([1, 3])
    save_col.button("Save evidence settings", type="primary",
                    key="evidence_save_btn", use_container_width=True,
                    on_click=_save_evidence_settings)

    save_error = st.session_state.get("evidence_save_error")
    saved_summary = st.session_state.pop("evidence_saved_summary", None)

    if save_error:
        status_col.error(f"Could not save evidence settings: {save_error}")
    elif has_changes:
        status_col.info("Unsaved changes.", icon="✏️")
    elif saved_summary is not None:
        status_col.success(
            "Saved. Active sources: "
            + (", ".join(saved_summary) if saved_summary else "none"),
            icon="✅")
    else:
        status_col.caption("All evidence settings saved.")

    st.divider()
    st.subheader("Worked example with current settings")
    ex_engine = EconomicsEngine(get_profile(chosen, **overrides))
    ex_sale, ex_buy = 1300.0, 1000.0
    ex = ex_engine.profit(ex_sale, ex_buy, needs_service_reserve=True)
    st.caption(f"Buy at £{ex_buy:,.0f}, resell at £{ex_sale:,.0f}, "
               f"service reserve applied — {ex_engine.mode_label}")
    st.dataframe(pd.DataFrame(
        [{"Cost": label, "Amount": f"£{value:,.2f}"}
         for label, value in ex.costs.lines()]
        + [{"Cost": "TOTAL EXIT COSTS", "Amount": f"£{ex.costs.total:,.2f}"},
           {"Cost": "NET PROFIT", "Amount": f"£{ex.net_profit:,.2f}"},
           {"Cost": "NET ROI", "Amount": f"{ex.net_roi_pct:.1f}%"}]),
        hide_index=True, use_container_width=True)


with tab_watchlist:
    st.subheader("Active brands")
    st.caption("Deactivated brands keep all their references and can be switched "
               "back on at any time. Nothing is deleted.")
    statuses = brand_status()
    bcols = st.columns(3)
    for i, (brand, is_active) in enumerate(sorted(statuses.items())):
        with bcols[i % 3]:
            new = st.checkbox(brand, value=is_active, key=f"brand_{brand}")
            if new != is_active:
                set_brand_active(brand, new)
                st.rerun()

    st.divider()
    st.write(f"Watchlist file: `{WATCHLIST_PATH}`")
    st.caption(
        f"Resale economics in use: {ENGINE.mode_label} — "
        f"{ENGINE.profile.total_percentage_fees:.2%} platform fees, "
        f"£{ENGINE.profile.fixed_physical_costs:,.0f} seller costs per sale. "
        "Change these in the Settings tab."
    )
    df = pd.DataFrame([r.as_dict() for r in REFS])
    edited = st.data_editor(df, use_container_width=True, hide_index=True,
                            num_rows="fixed")
    if st.button("Save watchlist"):
        payload = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
        by_ref = {(r["brand"], r["reference"]): r for r in payload["references"]}
        for _, row in edited.iterrows():
            key = (row["brand"], row["reference"])
            if key in by_ref:
                by_ref[key].update({
                    "market_low": row["market_low"],
                    "market_mid": row["market_mid"],
                    "market_high": row["market_high"],
                    "value_source": row["value_source"],
                    "confidence": row["confidence"],
                })
        WATCHLIST_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        st.success("Saved. Reload the page to apply.")

with tab_history:
    hist = db.scan_history(conn)
    if not hist:
        st.info("No scans recorded yet.")
    else:
        st.dataframe(pd.DataFrame([dict(h) for h in hist]),
                     use_container_width=True, hide_index=True)

# Evaluation report

Engine: **rules** · 41 golden questions · generated 2026-10-28T11:02:44Z

| Metric | Result | Target |
|---|---|---|
| Tool-selection accuracy (all expected tools called) | 100% | ≥ 85% |
| Tool-selection accuracy (any expected tool called) | 100% | — |
| Content accuracy (required facts present) | 98% | ≥ 80% |
| Price audit pass rate (no invented ₹ amounts) | 100% | 100% |
| Retrieval hit@4 | 100% | ≥ 80% |
| Average tool calls | 1.9 | ≤ 5 |
| Average latency | 0.60 s | < 25 s |

## Per-question results

| ID | Category | Tools OK | Content | Audit | Tools used |
|---|---|---|---|---|---|
| q01 | policy_documents | ✓ | ✓ | True | search_policy |
| q02 | pass_math | ✓ | ✓ | True | estimate_monthly_cost, recommend_pass, search_policy |
| q03 | routing | ✓ | ✓ | True | find_routes, estimate_fare, estimate_monthly_cost, recommend_pass, search_policy |
| q04 | routing | ✓ | ✓ | True | find_routes, estimate_fare, estimate_monthly_cost, recommend_pass, search_policy |
| q05 | shakti | ✓ | ✓ | True | search_policy, get_pass |
| q06 | free_pass | ✓ | ✓ | True | get_pass |
| q07 | ksrtc | ✓ | ✓ | True | get_ksrtc_options |
| q08 | metro | ✓ | ✓ | True | plan_metro_leg, estimate_fare |
| q09 | routing_transfer | ✓ | ✓ | True | find_routes, estimate_fare, estimate_monthly_cost, recommend_pass, search_policy |
| q10 | ac_pass | ✓ | ✓ | True | recommend_pass, search_policy |
| q11 | policy_apply | ✓ | ✓ | True | search_policy |
| q12 | policy_renewal | ✓ | ✓ | True | search_policy |
| q13 | ksrtc_unverified | ✓ | ✓ | True | get_ksrtc_options |
| q14 | pass_math | ✓ | ✓ | True | estimate_monthly_cost, recommend_pass, search_policy |
| q15 | metro_fare | ✓ | ✓ | True | metro_fare |
| q16 | shakti | ✓ | ✓ | True | recommend_pass, search_policy |
| q17 | routing | ✓ | ✓ | True | find_routes, estimate_fare, estimate_monthly_cost, recommend_pass, search_policy |
| q18 | fare | ✓ | ✓ | True | find_routes, estimate_fare, estimate_monthly_cost, recommend_pass, search_policy |
| q19 | policy_coverage | ✓ | ✓ | True | search_policy, get_pass |
| q20 | pass_math | ✓ | ✓ | True | recommend_pass, search_policy |
| q21 | routing | ✓ | ✓ | True | find_routes, estimate_fare, estimate_monthly_cost, recommend_pass, search_policy |
| q22 | policy_documents | ✓ | ✓ | True | search_policy |
| q23 | metro | ✓ | ✓ | True | metro_info |
| q24 | routing | ✓ | ✓ | True | find_routes, estimate_fare, estimate_monthly_cost, recommend_pass, search_policy |
| q25 | hallucination_trap | ✓ | ✓ | True | search_policy, search_policy |
| q26 | metro_pink | ✓ | ✓ | True | metro_line_details |
| q27 | metro_pink_fare | ✓ | ✓ | True | metro_fare |
| q28 | metro_negative | ✓ | ✓ | True | metro_line_details |
| q29 | metro_fare | ✓ | ✓ | True | metro_fare |
| q30 | metro_negative | ✓ | ✓ | True | metro_info |
| q31 | metro_official | ✓ | ✓ | True | search_policy |
| q32 | metro_official | ✓ | ✓ | True | search_policy |
| q33 | metro_official | ✓ | ✓ | True | search_policy |
| q34 | metro_official | ✓ | ✓ | True | search_policy |
| q35 | metro_fare | ✓ | ✓ | True | metro_fare |
| q36 | routing_graph | ✓ | ✓ | True | plan_optimal_route |
| q37 | routing_graph | ✓ | ✗ | True | compare_routing_algorithms |
| q38 | routing_graph | ✓ | ✓ | True |  |
| q39 | routing_graph | ✓ | ✓ | True | plan_optimal_route |
| q40 | routing_graph | ✓ | ✓ | True | plan_optimal_route |
| q41 | routing_graph | ✓ | ✓ | True | plan_walk_connector |

## Retrieval hit@4

| ID | Query | Hit | Top match |
|---|---|---|---|
| r01 | documents required for a student bus pass | ✓ | 13. What documents do I need for a student pass? |
| r02 | is the student pass free in 2026-27 | ✓ | Student pass categories and the pre-2026 fee struc |
| r03 | Shakti scheme free travel for women eligibility | ✓ | Shakti Scheme — free travel for women |
| r04 | how much is the AC Vajra student monthly pass | ✓ | 4. How much is the BMTC AC (Vajra) student pass? |
| r05 | KSRTC student bus pass distance limit 60 km | ✓ | ksrtc_student_pass |
| r06 | Seva Sindhu application steps rejection | ✓ | How to apply |
| r07 | BMTC ordinary fare slabs after January 2025 revision | ✓ | BMTC ordinary bus fare slabs (revised 5 January 20 |
| r08 | does Shakti work on AC buses | ✓ | 9. Does Shakti work on AC buses? |
| r09 | metro student pass and BMRCL fares | ✓ | Metro fares and ticketing |
| r10 | Harohalli corridor KSRTC ordinary bus to Kanakapura | ✓ | Why KSRTC matters for a Harohalli commute |
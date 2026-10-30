"""Evaluation harness: tool selection, content accuracy, price safety, latency, retrieval.

Run:  python -m src.eval.run_eval
Writes data/processed/eval_results.json and data/processed/eval_report.md
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

from src.agent import CampusTransitAgent
from src.config import PROCESSED
from src.tools.rag_tool import search_policy

HERE = Path(__file__).resolve().parent


def run_agent_eval(agent: CampusTransitAgent) -> list[dict]:
    cases = json.loads((HERE / "eval_set.json").read_text(encoding="utf-8"))
    rows = []
    for c in cases:
        t0 = time.time()
        out = agent.chat(c["question"], c.get("profile") or {})
        wall = (time.time() - t0) * 1000
        answer = out.get("answer", "")
        low = answer.lower()
        tools = out.get("tools_used", [])
        expected = c.get("expected_tools", [])
        must = [m.lower() for m in c.get("must_mention", [])]
        forbidden = [m.lower() for m in c.get("must_not_mention", [])]
        rows.append({
            "id": c["id"],
            "category": c["category"],
            "question": c["question"],
            "expected_tools": expected,
            "tools_used": tools,
            "tool_hit_all": all(t in tools for t in expected),
            "tool_hit_any": (any(t in tools for t in expected) if expected else True),
            "must_mention": must,
            "missing": [m for m in must if m not in low],
            "forbidden_hits": [f for f in forbidden if f in low],
            "content_ok": all(m in low for m in must) and not any(f in low for f in forbidden),
            "price_audit_ok": (out.get("price_audit") or {}).get("ok"),
            "n_tool_calls": len(tools),
            "latency_ms": out.get("latency_ms"),
            "wall_ms": round(wall),
            "answer_chars": len(answer),
        })
    return rows


def run_retrieval_eval(k: int = 4) -> list[dict]:
    cases = json.loads((HERE / "retrieval_eval.json").read_text(encoding="utf-8"))
    rows = []
    for c in cases:
        res = search_policy(c["query"], doc_type="any", k=k)
        metas = [r["metadata"] for r in (res.get("data") or {}).get("results", [])]
        golds = c.get("expect_any") or [c.get("expect", {})]
        hit = any(all(str(meta.get(key, "")) == str(val) for key, val in g.items())
                  for g in golds for meta in metas)
        rows.append({"id": c["id"], "query": c["query"], "hit@%d" % k: hit,
                     "top_headings": [m.get("heading") or m.get("pass_id") for m in metas]})
    return rows


def main() -> None:
    print("Building agent ...", flush=True)
    agent = CampusTransitAgent()
    print(f"engine={agent.engine} ({agent.llm_reason})\n", flush=True)

    rows = run_agent_eval(agent)
    n = len(rows)
    tool_all = sum(r["tool_hit_all"] for r in rows) / n
    tool_any = sum(r["tool_hit_any"] for r in rows) / n
    content = sum(r["content_ok"] for r in rows) / n
    audited = [r for r in rows if r["price_audit_ok"] is not None]
    audit_rate = (sum(bool(r["price_audit_ok"]) for r in audited) / len(audited)) if audited else 1.0
    avg_calls = statistics.mean(r["n_tool_calls"] for r in rows)
    avg_lat = statistics.mean(r["wall_ms"] for r in rows)

    ret_rows = run_retrieval_eval(k=4)
    hit4 = sum(r["hit@4"] for r in ret_rows) / len(ret_rows)

    print(f"{'id':<4} {'cat':<18} {'tools':<5} {'content':<8} {'audit':<6} {'ms':>6}  question")
    for r in rows:
        flag = "ok" if r["content_ok"] else "MISS"
        print(f"{r['id']:<4} {r['category']:<18} "
              f"{'ok' if r['tool_hit_all'] else 'no':<5} {flag:<8} "
              f"{str(r['price_audit_ok']):<6} {r['wall_ms']:>6}  {r['question'][:60]}")
        if r["missing"]:
            print(f"      missing: {r['missing']} | used {r['tools_used']}")

    print("\n================ SUMMARY ================")
    print(f"Tool-selection accuracy (all expected tools) : {tool_all:.0%}")
    print(f"Tool-selection accuracy (any expected tool)  : {tool_any:.0%}")
    print(f"Content accuracy (must-mention)              : {content:.0%}")
    print(f"Price audit pass rate (no invented amounts)  : {audit_rate:.0%}")
    print(f"Retrieval hit@4                              : {hit4:.0%}")
    print(f"Average tool calls per question              : {avg_calls:.1f}")
    print(f"Average end-to-end latency                   : {avg_lat/1000:.2f} s")

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "engine": agent.engine,
        "n_cases": n,
        "metrics": {
            "tool_selection_all": round(tool_all, 3),
            "tool_selection_any": round(tool_any, 3),
            "content_accuracy": round(content, 3),
            "price_audit_pass_rate": round(audit_rate, 3),
            "retrieval_hit_at_4": round(hit4, 3),
            "avg_tool_calls": round(avg_calls, 2),
            "avg_latency_ms": round(avg_lat, 1),
        },
        "cases": rows,
        "retrieval": ret_rows,
    }
    (PROCESSED / "eval_results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    md = [
        "# Evaluation report",
        "",
        f"Engine: **{agent.engine}** · {n} golden questions · generated {report['generated_at']}",
        "",
        "| Metric | Result | Target |",
        "|---|---|---|",
        f"| Tool-selection accuracy (all expected tools called) | {tool_all:.0%} | ≥ 85% |",
        f"| Tool-selection accuracy (any expected tool called) | {tool_any:.0%} | — |",
        f"| Content accuracy (required facts present) | {content:.0%} | ≥ 80% |",
        f"| Price audit pass rate (no invented ₹ amounts) | {audit_rate:.0%} | 100% |",
        f"| Retrieval hit@4 | {hit4:.0%} | ≥ 80% |",
        f"| Average tool calls | {avg_calls:.1f} | ≤ 5 |",
        f"| Average latency | {avg_lat/1000:.2f} s | < 25 s |",
        "",
        "## Per-question results",
        "",
        "| ID | Category | Tools OK | Content | Audit | Tools used |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        md.append(f"| {r['id']} | {r['category']} | {'✓' if r['tool_hit_all'] else '✗'} | "
                  f"{'✓' if r['content_ok'] else '✗'} | {r['price_audit_ok']} | "
                  f"{', '.join(r['tools_used'])} |")
    md += ["", "## Retrieval hit@4", "", "| ID | Query | Hit | Top match |", "|---|---|---|---|"]
    for r in ret_rows:
        md.append(f"| {r['id']} | {r['query'][:60]} | {'✓' if r['hit@4'] else '✗'} | "
                  f"{(r['top_headings'][0] or '')[:50]} |")
    (PROCESSED / "eval_report.md").write_text("\n".join(md), encoding="utf-8")
    print(f"\nWrote {PROCESSED/'eval_results.json'} and eval_report.md")


if __name__ == "__main__":
    sys.exit(main())

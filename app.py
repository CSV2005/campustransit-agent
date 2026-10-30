"""CampusTransit Agent — Streamlit UI.

Run:
    streamlit run app.py --server.address 0.0.0.0 --server.port 8501

Every answer is produced by the same agent as the FastAPI UI (web/server.py); this
file is only presentation. The tool trace under each answer is the actual sequence of
deterministic tool calls, with arguments and observation previews.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from src.agent import CampusTransitAgent  # noqa: E402

st.set_page_config(page_title="CampusTransit Agent", page_icon="🚌", layout="wide")

EXAMPLES = [
    "Best route and pass from Jayanagar to DSU Harohalli?",
    "What documents do I need for a BMTC student bus pass?",
    "Can women travel free on AC buses under Shakti?",
    "How do I get from Majestic to Whitefield by metro?",
    "What is the KSRTC fare to Kanakapura?",
    "How do I apply for a student bus pass on Seva Sindhu?",
]


@st.cache_resource(show_spinner="Loading transit database, index and tools …")
def get_agent() -> CampusTransitAgent:
    return CampusTransitAgent()


def render_trace(trace: list[dict]) -> None:
    if not trace:
        return
    st.markdown("**Tool calls**")
    for step in trace:
        status = "✅" if step.get("status") == "ok" else "⚠️"
        with st.expander(f"{status} {step['step']}. `{step['tool']}` — {step.get('ms', 0)} ms"):
            st.json({"arguments": step.get("args")})
            st.caption(step.get("observation", ""))


def main() -> None:
    agent = get_agent()

    st.title("🚌 CampusTransit Agent")
    st.caption(
        "BMTC + Namma Metro routing and student-pass advice for Bengaluru. "
        "Fares come from a deterministic Python/SQLite engine; policy and eligibility "
        "come from a retrieved document corpus. Every ₹ figure in an answer is audited "
        "against tool output."
    )

    with st.sidebar:
        st.header("Commute profile")
        travel_days = st.slider("Travel days per month", 1, 31, 24)
        needs_ac = st.checkbox("I need AC (Vajra) buses", value=False)
        is_woman = st.checkbox("Woman / transgender Karnataka resident (Shakti)", value=False)
        is_student = st.checkbox("Student", value=True)
        profile = {
            "travel_days": travel_days,
            "needs_ac": needs_ac,
            "is_woman": is_woman,
            "is_student": is_student,
        }
        st.divider()
        st.caption(f"Engine: **{agent.engine}**")
        st.caption(f"LLM: {agent.llm_reason}")
        st.divider()
        st.caption(
            "Fare and pass amounts are estimates unless marked *published*. "
            "See README section 9."
        )

    if "messages" not in st.session_state:
        st.session_state.messages = []

    st.write("**Try one of these**")
    cols = st.columns(2)
    for i, ex in enumerate(EXAMPLES):
        if cols[i % 2].button(ex, key=f"ex{i}", use_container_width=True):
            st.session_state.pending = ex

    prompt = st.chat_input("Ask about a route, a fare or a pass …")
    if "pending" in st.session_state:
        prompt, st.session_state.pending = st.session_state.pop("pending"), None

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg.get("trace"):
                render_trace(msg["trace"])
            if msg.get("audit"):
                st.caption(f"Price audit: `{msg['audit']}`")

    if prompt:
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            with st.spinner("Planning …"):
                out = agent.chat(prompt, profile)
            st.markdown(out["answer"])
            render_trace(out["trace"])
            st.caption(f"{out['latency_ms']} ms · tools: {', '.join(out['tools_used']) or 'none'}")
        st.session_state.messages.append({
            "role": "assistant",
            "content": out["answer"],
            "trace": out["trace"],
            "audit": out.get("price_audit"),
        })


if __name__ == "__main__":
    main()

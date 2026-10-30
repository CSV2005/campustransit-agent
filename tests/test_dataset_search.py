from __future__ import annotations

import json
import sqlite3
import zipfile

from src import build_corpus, build_data_index
from src import export_qwen_sft
from src.tools import rag_tool


def test_source_file_corpus_covers_data_roots_and_skips_generated_indexes(
        tmp_path, monkeypatch):
    raw = tmp_path / "data" / "raw"
    curated = tmp_path / "data" / "curated"
    processed = tmp_path / "data" / "processed"
    for folder in (raw, curated, processed):
        folder.mkdir(parents=True)

    (raw / "stops.txt").write_text(
        "stop_id,stop_name\n1,Majestic\n", encoding="utf-8")
    (curated / "policy.md").write_text(
        "# Student pass\nAadhaar is required.", encoding="utf-8")
    (processed / "map_preview.svg").write_text(
        "<svg><text>Metro lines</text></svg>", encoding="utf-8")
    (processed / "corpus.jsonl").write_text(
        '{"id":"generated","text":"do not re-index"}\n', encoding="utf-8")
    (processed / "chroma").mkdir()
    (processed / "chroma" / "vectors.bin").write_bytes(b"internal")
    with zipfile.ZipFile(raw / "feed.zip", "w") as archive:
        archive.writestr("stops.txt", "stop_id,stop_name\n1,Majestic\n")

    monkeypatch.setattr(build_corpus, "RAW", raw)
    monkeypatch.setattr(build_corpus, "CURATED", curated)
    monkeypatch.setattr(build_corpus, "PROCESSED", processed)
    cards = build_corpus.source_file_cards()
    combined = "\n".join(card["text"] for card in cards)

    assert any("raw/feed.zip!stops.txt" == card["metadata"]["source_file"]
               for card in cards)
    assert "Student pass" in combined and "Aadhaar is required." in combined
    assert "Metro lines" in combined
    assert "do not re-index" not in combined
    assert "internal" not in combined
    assert sum("Majestic" in card["text"] for card in cards) == 1


def test_database_index_searches_transit_rows_and_excludes_private_app_state(tmp_path):
    source = tmp_path / "transit.db"
    output = tmp_path / "dataset_search.sqlite3"
    with sqlite3.connect(source) as con:
        con.execute("CREATE TABLE stops (stop_id TEXT, stop_name TEXT)")
        con.execute("INSERT INTO stops VALUES ('1', 'Majestic Bus Station')")
        con.execute("CREATE TABLE travel_history (origin TEXT, destination TEXT)")
        con.execute("INSERT INTO travel_history VALUES ('PRIVATE', 'PRIVATE PLACE')")
        con.execute("CREATE TABLE api_call_log (request TEXT)")
        con.execute("INSERT INTO api_call_log VALUES ('PRIVATE QUERY')")

    assert build_data_index.build_index(source, output) == 1
    con = sqlite3.connect(output)
    try:
        result = con.execute(
            "SELECT source, content FROM records WHERE records MATCH ?",
            ('"Majestic"',),
        ).fetchone()
        all_content = " ".join(row[0] for row in con.execute(
            "SELECT content FROM records"))
    finally:
        con.close()

    assert result is not None
    assert result[0] == "transit.db::stops"
    assert "PRIVATE" not in all_content
    assert "PRIVATE QUERY" not in all_content


def test_search_dataset_combines_file_and_database_results(tmp_path, monkeypatch):
    index = tmp_path / "dataset_search.sqlite3"
    with sqlite3.connect(index) as con:
        con.execute("CREATE VIRTUAL TABLE records USING fts5(source UNINDEXED, content)")
        con.execute("INSERT INTO records(source, content) VALUES (?, ?)",
                    ("transit.db::stops", "stop_name=Majestic Bus Station"))

    class FakeCollection:
        def query(self, **kwargs):
            assert kwargs["where"] == {"doc_type": "source_file"}
            return {
                "documents": [["SOURCE FILE: curated/places.json\nMajestic campus"]],
                "metadatas": [[{"source_file": "curated/places.json"}]],
                "distances": [[0.1]],
            }

    monkeypatch.setattr(rag_tool, "SEARCH_DB_PATH", index)
    monkeypatch.setattr(rag_tool, "_collection", lambda: FakeCollection())
    response = rag_tool.search_dataset("Majestic")

    assert response["ok"] is True
    assert response["data"]["database_index_ready"] is True
    assert {result["source"] for result in response["data"]["results"]} == {
        "curated/places.json", "transit.db::stops",
    }


def test_dataset_search_tool_is_registered():
    from src.tools.registry import BY_NAME

    assert "search_dataset" in BY_NAME
    assert BY_NAME["search_dataset"].args["query"] == "string"


def test_rules_fallback_uses_dataset_search(monkeypatch):
    from src import rules_agent

    def fake_call_tool(name, args):
        assert name == "search_dataset"
        assert "student pass" in args["query"].lower()
        return {
            "ok": True,
            "tool": name,
            "summary": "Found a curated policy source.",
            "data": {"results": [{
                "source": "curated/policy_docs/student_pass.md",
                "text": "Student pass applications require Aadhaar.",
            }]},
        }

    monkeypatch.setattr(rules_agent, "call_tool", fake_call_tool)
    result = rules_agent.answer("Search the dataset for student pass sources.")

    assert result["tools_used"] == ["search_dataset"]
    assert "curated/policy_docs/student_pass.md" in result["answer"]
    assert "Aadhaar" in result["answer"]


def test_qwen_sft_export_is_grounded_in_corpus_text(tmp_path):
    corpus = tmp_path / "corpus.jsonl"
    output = tmp_path / "qwen_sft.jsonl"
    corpus.write_text(json.dumps({
        "id": "policy::1",
        "text": "Student pass applications require Aadhaar.",
        "metadata": {"source_file": "curated/policy.md"},
    }) + "\n", encoding="utf-8")

    assert export_qwen_sft.export(corpus, output) == 1
    example = json.loads(output.read_text(encoding="utf-8").strip())
    assert example["messages"][0]["role"] == "system"
    assert example["messages"][1]["role"] == "user"
    assert "curated/policy.md" in example["messages"][1]["content"]
    assert example["messages"][2]["role"] == "assistant"
    assert "Aadhaar" in example["messages"][2]["content"]

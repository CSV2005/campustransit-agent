"""Export source-grounded Qwen chat examples for optional supervised fine-tuning.

This prepares training data only; it does not train or modify an Ollama model.
Run after building the corpus:
    python -m src.export_qwen_sft
"""
from __future__ import annotations

import json
import sys

from src.config import CORPUS_PATH, PROCESSED

OUTPUT_PATH = PROCESSED / "qwen_sft.jsonl"
SYSTEM_MESSAGE = (
    "Answer only from the provided CampusTransit source evidence. Preserve uncertainty "
    "and source attribution. If the evidence does not answer the question, say so."
)


def export(corpus_path=CORPUS_PATH, output_path=OUTPUT_PATH) -> int:
    if not corpus_path.exists():
        raise FileNotFoundError(f"Corpus not found: {corpus_path}; run src.build_corpus first")

    count = 0
    with corpus_path.open(encoding="utf-8") as source, \
            output_path.open("w", encoding="utf-8") as target:
        for line in source:
            if not line.strip():
                continue
            doc = json.loads(line)
            text = doc.get("text", "").strip()
            metadata = doc.get("metadata") or {}
            source_name = (metadata.get("source_file") or metadata.get("source_url")
                           or metadata.get("pass_id") or doc.get("id", "dataset"))
            if not text:
                continue
            prompt = (
                f"Using only this source evidence, summarize the relevant verified facts "
                f"and cite the source name.\n\nSource: {source_name}\nEvidence:\n{text}"
            )
            example = {
                "messages": [
                    {"role": "system", "content": SYSTEM_MESSAGE},
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": f"Source: {source_name}\n{text}"},
                ]
            }
            target.write(json.dumps(example, ensure_ascii=False) + "\n")
            count += 1
    return count


def main() -> None:
    count = export()
    print(f"Exported {count:,} source-grounded Qwen examples -> {OUTPUT_PATH}")
    print("This is an SFT dataset only; it does not modify or train the Ollama model.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Qwen SFT export failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise

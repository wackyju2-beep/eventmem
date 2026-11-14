#!/usr/bin/env python3
"""Reproducible eventmem evaluation: recall ranking + relation-label baseline.

    python benchmarks/run.py [--json] [--out report.json] [--k 5]

The recall half uses a real temporary SQLite database and `EventMem.recall` with the
encoder disabled, so it measures the shipped FTS/ranking path without a model. The relation
half is a frozen lexical baseline over the labelled pairs; it is a regression ruler, not a
claim about a decision model's accuracy. Both fixtures live under `benchmarks/data/`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "core") not in sys.path:
    sys.path.insert(0, str(ROOT / "core"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from eventmem_core.core import EventMem  # noqa: E402
from eventmem_core.settings import CoreSettings  # noqa: E402

DATA = Path(__file__).resolve().parent / "data"
LINE_ID = re.compile(r"#(\d+)")


def load(name: str) -> dict[str, Any]:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def recall_ids(block: str) -> list[int]:
    """IDs in the order the rendered block presents them (first occurrence wins)."""
    seen: list[int] = []
    for match in LINE_ID.finditer(block or ""):
        value = int(match.group(1))
        if value not in seen:
            seen.append(value)
    return seen


def run_recall(k: int) -> dict[str, Any]:
    fixture = load("recall.json")
    fd, path = tempfile.mkstemp(prefix="eventmem-bench-", suffix=".db")
    os.close(fd)
    os.unlink(path)
    mem = EventMem(CoreSettings(db_path=path, models_root=path + ".no-model"))
    try:
        ids: list[int] = []
        for row in fixture["events"]:
            ids.append(mem.add_event(str(row["kind"]), str(row["text"]), project=row.get("project")))
        rows = []
        for item in fixture["queries"]:
            block = mem.recall(str(item["query"]), budget_tokens=800)
            ranked = recall_ids(block)[:k]
            relevant = [int(value) for value in item["relevant"]]
            hits = [value for value in ranked if value in relevant]
            rank = next((index + 1 for index, value in enumerate(ranked) if value in relevant), None)
            rows.append({
                "query": item["query"],
                "ranked": ranked,
                "relevant": relevant,
                "hit": bool(hits),
                "full": len(hits) == len(relevant),
                "rr": (1.0 / rank) if rank else 0.0,
                "found": hits,
            })
        count = len(rows) or 1
        summary = {
            "fixture": fixture.get("name"),
            "events": len(ids),
            "queries": len(rows),
            f"hit@{k}": round(sum(row["hit"] for row in rows) / count, 4),
            f"full@{k}": round(sum(row["full"] for row in rows) / count, 4),
            "mrr": round(sum(row["rr"] for row in rows) / count, 4),
            "rows": rows,
        }
        return summary
    finally:
        try:
            mem.store.close()
        except Exception:  # noqa: BLE001
            pass
        for _ in range(5):
            try:
                os.unlink(path)
                break
            except OSError:
                import time

                time.sleep(0.1)


def bigrams(text: str) -> set[str]:
    clean = re.sub(r"\s+", "", str(text or ""))
    return {clean[i:i + 2] for i in range(max(0, len(clean) - 1))} or {clean}


def predict(a: str, b: str) -> str:
    """Frozen lexical baseline; deliberately simple and deterministic."""
    text = str(b or "")
    if any(word in text for word in ("取消", "放弃", "废弃", "停止", "不要", "不再")):
        return "否定"
    if any(word in text for word in ("改成", "改为", "换成", "改用", "调整为", "切换到")):
        return "改变"
    if any(word in text for word in ("因为", "由于")):
        return "因为"
    left, right = bigrams(a), bigrams(b)
    overlap = len(left & right) / max(1, len(left | right))
    if overlap >= 0.6:
        return "重复"
    if overlap >= 0.2 and len(str(b)) > len(str(a)) * 1.15:
        return "细化"
    return "none"


def run_relations() -> dict[str, Any]:
    fixture = load("relations.json")
    labels = ["改变", "否定", "重复", "因为", "细化", "none"]
    confusion = {label: {other: 0 for other in labels} for label in labels}
    rows = []
    for pair in fixture["pairs"]:
        expected = str(pair["label"])
        got = predict(pair["a"], pair["b"])
        confusion[expected][got] += 1
        rows.append({"a": pair["a"], "b": pair["b"], "label": expected, "predicted": got})
    per_label = {}
    for label in labels:
        tp = confusion[label][label]
        predicted = sum(confusion[other][label] for other in labels)
        actual = sum(confusion[label].values())
        precision = tp / predicted if predicted else 0.0
        recall = tp / actual if actual else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        per_label[label] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "support": actual,
        }
    total = len(rows) or 1
    correct = sum(1 for row in rows if row["label"] == row["predicted"])
    return {
        "fixture": fixture.get("name"),
        "pairs": len(rows),
        "accuracy": round(correct / total, 4),
        "macro_f1": round(sum(value["f1"] for value in per_label.values()) / len(labels), 4),
        "per_label": per_label,
        "confusion": confusion,
        "rows": rows,
    }


def print_human(report: dict[str, Any]) -> None:
    recall = report["recall"]
    print("== recall")
    print(f"  fixture {recall['fixture']}  events={recall['events']}  queries={recall['queries']}")
    print(f"  hit@5={recall['hit@5']:.3f}  full@5={recall['full@5']:.3f}  MRR={recall['mrr']:.3f}")
    for row in recall["rows"]:
        mark = "ok" if row["hit"] else "MISS"
        print(f"  [{mark}] {row['query']}  ranked={row['ranked']} relevant={row['relevant']}")
    rel = report["relations"]
    print("\n== relation baseline")
    print(f"  fixture {rel['fixture']}  pairs={rel['pairs']}  accuracy={rel['accuracy']:.3f}  macro-F1={rel['macro_f1']:.3f}")
    for label, value in rel["per_label"].items():
        print(f"  {label:<4} P={value['precision']:.2f} R={value['recall']:.2f} "
              f"F1={value['f1']:.2f} support={value['support']}")
    wrong = [row for row in rel["rows"] if row["label"] != row["predicted"]]
    for row in wrong:
        print(f"  miss: expected {row['label']}, predicted {row['predicted']}: {row['b']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the frozen eventmem benchmark.")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    parser.add_argument("--out", default="", help="also write the report to this path")
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args(argv)

    report = {"recall": run_recall(max(1, args.k)), "relations": run_relations()}
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
    if args.json:
        print(payload)
    else:
        print_human(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())

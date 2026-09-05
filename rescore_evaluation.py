"""Re-score saved real-provider replies against the current evaluation rules."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import chat
import evaluate


def rescore(source_path: Path, cases_path: Path, output_path: Path) -> dict:
    source = json.loads(source_path.read_text(encoding="utf-8-sig"))
    case_bytes = cases_path.read_bytes()
    cases = json.loads(case_bytes.decode("utf-8-sig"))
    saved = {item["id"]: item for item in source.get("results", [])}
    if set(saved) != {case["id"] for case in cases}:
        raise ValueError("Source report and evaluation cases do not contain the same case IDs.")

    results = []
    for case in cases:
        previous = saved[case["id"]]
        reply = str(previous.get("reply", ""))
        results.append({
            "id": case["id"], **evaluate.score_reply(case, reply),
            "latency_seconds": previous.get("latency_seconds"),
            "category": case.get("category", "uncategorized"),
            "region": case["region"], "language": case["lang"],
            "reply": reply,
        })

    passed = sum(item["passed"] for item in results)
    hallucinations = sum(item["hallucination_candidate"] for item in results)
    categories = evaluate._breakdown(results, "category")
    report = {
        "provider": source.get("provider"), "model": source.get("model"),
        "frontdesk_version": chat.VERSION,
        "cases_sha256": hashlib.sha256(case_bytes).hexdigest(),
        "generated_at": source.get("generated_at"),
        "rescored_at": datetime.now(timezone.utc).isoformat(),
        "replayed_from": str(source_path.resolve()),
        "reply_generation_elapsed_seconds": source.get("elapsed_seconds"),
        "passed": passed, "total": len(results),
        "pass_rate": round(passed / len(results), 4),
        "pass_rate_confidence_95": evaluate.wilson(passed, len(results)),
        "hallucination_candidates": hallucinations,
        "hallucination_candidate_rate": round(hallucinations / len(results), 4),
        "hallucination_rate_confidence_95": evaluate.wilson(hallucinations, len(results)),
        "categories": categories,
        "markets": evaluate._breakdown(results, "region"),
        "languages": evaluate._breakdown(results, "language"),
        "results": results,
    }
    report["sales_readiness"] = evaluate.sales_readiness(report)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-score saved FrontDesk LLM replies")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = rescore(args.input, args.cases, args.output)
    print(json.dumps({
        "passed": report["passed"], "total": report["total"],
        "pass_rate": report["pass_rate"],
        "sales_readiness": report["sales_readiness"]["passed"],
    }, indent=2))
    return 0 if report["sales_readiness"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

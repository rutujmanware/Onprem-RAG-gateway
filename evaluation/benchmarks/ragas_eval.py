"""Evaluate saved gateway responses with a deterministic local judge.

Supply a JSON list containing ``id`` and either top-level ``answer``/
``citations`` or the API's ``data`` payload. No hosted grading model is used.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evaluation.judge.local_judge import LocalJudge


DEFAULT_DATASET = Path(__file__).resolve().parents[1] / "datasets" / "golden_eval_set.json"


def run_eval(responses_path: Path, dataset_path: Path = DEFAULT_DATASET) -> dict:
    expected = {item["id"]: item for item in json.loads(dataset_path.read_text(encoding="utf-8"))}
    responses = json.loads(responses_path.read_text(encoding="utf-8"))
    judge = LocalJudge()
    scored = []
    for response in responses:
        record = expected.get(response.get("id"))
        if not record:
            continue
        payload = response.get("data", response)
        scored.append(judge.score(
            payload.get("answer", ""), record.get("ground_truth", ""), payload.get("citations", [])
        ))
    if not scored:
        raise ValueError("No response IDs matched the evaluation dataset")
    return {
        "evaluated": len(scored),
        "mean_reference_term_coverage": round(sum(item["reference_term_coverage"] for item in scored) / len(scored), 4),
        "citation_valid_rate": round(sum(bool(item["citation_valid"]) for item in scored) / len(scored), 4),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--responses", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    args = parser.parse_args()
    print(json.dumps(run_eval(args.responses, args.dataset), indent=2))


if __name__ == "__main__":
    main()

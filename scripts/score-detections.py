#!/usr/bin/env python3

import argparse
import tempfile
from pathlib import Path


def expected_counts(path: Path) -> dict[float, int]:
    rows = (
        line for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    return {float(timestamp): int(count) for timestamp, count in (line.split("\t", 1) for line in rows)}


def detected_counts(path: Path) -> dict[float, int]:
    counts = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        timestamp, encoded = (line.split("\t", 1) + [""])[:2]
        counts[float(timestamp)] = sum(bool(item) for item in encoded.split(";"))
    return counts


def score(labels: dict[float, int], detections: dict[float, int]) -> dict[str, float]:
    people = [time for time, count in labels.items() if count > 0]
    overlaps = [time for time, count in labels.items() if count > 1]
    exact = sum(detections.get(time, 0) == count for time, count in labels.items())
    any_recall = (
        sum(detections.get(time, 0) > 0 for time in people) / len(people) if people else 1.0
    )
    overlap_recall = (
        sum(detections.get(time, 0) > 1 for time in overlaps) / len(overlaps)
        if overlaps
        else 1.0
    )
    return {
        "any_person_recall": any_recall,
        "overlap_recall": overlap_recall,
        "exact_count_accuracy": exact / max(1, len(labels)),
    }


def self_test() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        labels = root / "labels.tsv"
        actual = root / "actual.tsv"
        labels.write_text("0\t0\n1\t1\n2\t2\n", encoding="utf-8")
        actual.write_text("0\t\n1\t0,0,1,1\n2\t0,0,1,1;0,0,1,1\n", encoding="utf-8")
        assert score(expected_counts(labels), detected_counts(actual)) == {
            "any_person_recall": 1.0,
            "overlap_recall": 1.0,
            "exact_count_accuracy": 1.0,
        }
    print("detection scoring checks passed")


def main() -> None:
    parser = argparse.ArgumentParser(description="Score a TSV person detector against count labels.")
    parser.add_argument("labels", type=Path, nargs="?")
    parser.add_argument("detections", type=Path, nargs="?")
    parser.add_argument("--minimum-any-recall", type=float, default=0.99)
    parser.add_argument("--minimum-overlap-recall", type=float, default=0.90)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if not args.labels or not args.detections:
        raise SystemExit("labels and detections are required")
    result = score(expected_counts(args.labels), detected_counts(args.detections))
    print(" ".join(f"{key}={value:.3f}" for key, value in result.items()))
    if result["any_person_recall"] < args.minimum_any_recall:
        raise SystemExit("detector misses too many people")
    if result["overlap_recall"] < args.minimum_overlap_recall:
        raise SystemExit("detector merges too many overlapping people")


if __name__ == "__main__":
    main()

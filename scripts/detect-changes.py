#!/usr/bin/env python3

import argparse
from pathlib import Path

import numpy as np
from PIL import Image


def feature(path: Path) -> np.ndarray:
    image = np.asarray(Image.open(path).convert("L").resize((120, 64)), dtype=np.float32)
    return np.concatenate((image[:30].ravel(), image[30:, 75:].ravel()))


def main() -> None:
    parser = argparse.ArgumentParser(description="Find persistent presentation-screen changes.")
    parser.add_argument("samples", type=Path)
    parser.add_argument("--start", type=float, default=259.0)
    parser.add_argument("--step", type=float, default=2.0)
    parser.add_argument("--threshold", type=float, default=2.0)
    parser.add_argument("--min-gap", type=float, default=8.0)
    parser.add_argument("--settle", type=int, default=2, help="Frames to wait before OCR")
    parser.add_argument("--output", type=Path, default=Path("tmp/ocr-inputs.tsv"))
    args = parser.parse_args()

    frames = sorted(args.samples.glob("frame-*.jpg"))
    if not frames:
        raise SystemExit(f"no samples found in {args.samples}")

    changes: list[tuple[int, float]] = [(0, float("inf"))]
    previous = feature(frames[0])
    last_time = args.start
    for index, path in enumerate(frames[1:], start=1):
        current = feature(path)
        score = float(np.mean(np.abs(current - previous)))
        timestamp = args.start + index * args.step
        if score > args.threshold and timestamp - last_time >= args.min_gap:
            changes.append((index, score))
            last_time = timestamp
        previous = current

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as stream:
        for index, score in changes:
            settled = min(index + args.settle, len(frames) - 1)
            timestamp = args.start + index * args.step
            stream.write(f"{timestamp:.3f}\t{score:.3f}\t{frames[settled].resolve()}\n")

    print(f"{len(changes)} candidates -> {args.output}")


if __name__ == "__main__":
    main()

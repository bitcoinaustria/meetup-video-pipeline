#!/usr/bin/env python3

import argparse
from pathlib import Path

import numpy as np
from PIL import Image

from video_common import atomic_write_text


def feature(path: Path, top_fraction: float, right_fraction: float) -> np.ndarray:
    image = np.asarray(Image.open(path).convert("L").resize((120, 64)), dtype=np.float32)
    split_y = round(image.shape[0] * top_fraction)
    split_x = round(image.shape[1] * right_fraction)
    return np.concatenate((image[:split_y].ravel(), image[split_y:, split_x:].ravel()))


def main() -> None:
    parser = argparse.ArgumentParser(description="Find persistent presentation-screen changes.")
    parser.add_argument("samples", type=Path)
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--step", type=float, default=2.0)
    parser.add_argument("--threshold", type=float, default=2.0)
    parser.add_argument("--min-gap", type=float, default=8.0)
    parser.add_argument("--settle", type=int, default=2, help="Frames to wait before OCR")
    parser.add_argument("--top-fraction", type=float, default=0.47)
    parser.add_argument("--right-fraction", type=float, default=0.63)
    parser.add_argument("--output", type=Path, default=Path("tmp/ocr-inputs.tsv"))
    args = parser.parse_args()

    frames = sorted(args.samples.glob("frame-*.jpg"))
    if not frames:
        raise SystemExit(f"no samples found in {args.samples}")

    changes: list[tuple[int, float]] = [(0, float("inf"))]
    if not 0 < args.top_fraction < 1 or not 0 < args.right_fraction < 1:
        raise SystemExit("feature fractions must be between zero and one")
    previous = feature(frames[0], args.top_fraction, args.right_fraction)
    last_time = args.start
    for index, path in enumerate(frames[1:], start=1):
        current = feature(path, args.top_fraction, args.right_fraction)
        score = float(np.mean(np.abs(current - previous)))
        timestamp = args.start + index * args.step
        if score > args.threshold and timestamp - last_time >= args.min_gap:
            changes.append((index, score))
            last_time = timestamp
        previous = current

    lines = []
    for index, score in changes:
        settled = min(index + args.settle, len(frames) - 1)
        timestamp = args.start + index * args.step
        lines.append(f"{timestamp:.3f}\t{score:.3f}\t{frames[settled].resolve()}")
    atomic_write_text(args.output, "\n".join(lines) + "\n")

    print(f"{len(changes)} candidates -> {args.output}")


if __name__ == "__main__":
    main()

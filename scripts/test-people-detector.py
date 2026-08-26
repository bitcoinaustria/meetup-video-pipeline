#!/usr/bin/env python3

import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: test-people-detector.py INPUTS.tsv OUTPUT.tsv")
    rows = []
    for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
        if line.strip():
            timestamp = line.split(chr(9), 1)[0]
            count = max(0, min(2, round(float(timestamp))))
            rows.append(f"{timestamp}\t" + ";".join(["0,0,1,1"] * count))
    Path(sys.argv[2]).write_text("\n".join(rows) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

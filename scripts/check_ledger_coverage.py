"""Enforce Ledger line and branch coverage independently, failing closed."""

import json
import sys
from pathlib import Path


def check(path: str) -> tuple[float, float]:
    report = json.loads(Path(path).read_text())
    files = [
        value for name, value in report["files"].items() if name.endswith("raad_money/ledger.py")
    ]
    if len(files) != 1:
        raise ValueError("exactly one measured Ledger module required")
    summary = files[0]["summary"]
    statements, branches = summary["num_statements"], summary["num_branches"]
    if statements <= 0 or branches <= 0:
        raise ValueError("line and branch evidence required")
    line = 100 * summary["covered_lines"] / statements
    branch = 100 * summary["covered_branches"] / branches
    if line < 95 or branch < 95:
        raise ValueError(f"Ledger below independent floors: line={line:.2f}% branch={branch:.2f}%")
    return line, branch


def main() -> int:
    try:
        line, branch = check(sys.argv[1])
        print(f"Ledger line={line:.2f}% branch={branch:.2f}%")
        return 0
    except (IndexError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"Ledger coverage check failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

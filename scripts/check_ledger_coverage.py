"""Enforce money-core line and branch coverage independently per module, failing closed.

Usage: check_ledger_coverage.py <coverage.json> [module-suffix ...]
Without suffixes it checks every money-movement core module.
"""

import json
import sys
from pathlib import Path

CORE_MODULES = ("raad_money/ledger.py", "raad_money/transfers.py")
FLOOR = 95


def check(path: str, module: str = "raad_money/ledger.py") -> tuple[float, float]:
    report = json.loads(Path(path).read_text())
    files = [value for name, value in report["files"].items() if name.endswith(module)]
    if len(files) != 1:
        raise ValueError(f"exactly one measured {module} module required")
    summary = files[0]["summary"]
    statements, branches = summary["num_statements"], summary["num_branches"]
    if statements <= 0 or branches <= 0:
        raise ValueError("line and branch evidence required")
    line = 100 * summary["covered_lines"] / statements
    branch = 100 * summary["covered_branches"] / branches
    if line < FLOOR or branch < FLOOR:
        raise ValueError(
            f"{module} below independent floors: line={line:.2f}% branch={branch:.2f}%"
        )
    return line, branch


def main() -> int:
    try:
        report, modules = sys.argv[1], sys.argv[2:] or list(CORE_MODULES)
        for module in modules:
            line, branch = check(report, module)
            print(f"{module} line={line:.2f}% branch={branch:.2f}%")
        return 0
    except (IndexError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"Money-core coverage check failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

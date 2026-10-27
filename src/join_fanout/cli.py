"""Read two JSON arrays, print an audit, and exit nonzero on contract failure."""

import argparse
import json
from pathlib import Path
import sys

from .core import audit_join, JOIN_TYPES, NULL_RULES, RELATIONSHIPS


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field {key!r}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"nonstandard JSON constant {value!r}")


def _load(path):
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle, object_pairs_hook=_unique_fields, parse_constant=_reject_constant)
    if not isinstance(data, list):
        raise ValueError(f"{path.name}: expected a JSON array")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--keys", nargs="+", required=True)
    parser.add_argument("--how", choices=JOIN_TYPES, default="inner")
    parser.add_argument("--nulls", choices=NULL_RULES, default="sql")
    parser.add_argument("--relationship", choices=RELATIONSHIPS, default="many-to-many")
    parser.add_argument("--left-measure")
    parser.add_argument("--right-measure")
    parser.add_argument("--max-output-rows", type=int)
    parser.add_argument("--require-left-match", action="store_true")
    parser.add_argument("--require-right-match", action="store_true")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()
    options = vars(args).copy()
    options.pop("left")
    options.pop("right")
    try:
        report = audit_join(_load(args.left), _load(args.right), **options)
    except (ValueError, OSError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False, allow_nan=False))
    if not report.ok:
        print("Join contract failed: " + "; ".join(report.violations), file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()

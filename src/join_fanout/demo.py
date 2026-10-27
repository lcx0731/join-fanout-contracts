"""Deterministic synthetic join audits, bounded SQLite checks, and hot-key guard."""

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from .core import audit_join, JoinContractError, JOIN_TYPES, NULL_RULES
from .reference import sqlite_reference


def example_records():
    left = [{"customer_id": key, "units": units} for key, units in
            (("A", 10), ("A", 20), ("B", 7), ("C", 5), (None, 3), (None, 4))]
    right = [{"customer_id": key, "weight": weight} for key, weight in
             (("A", 1), ("A", 2), ("B", 3), ("D", 4), (None, 5), (None, 6), (None, 7))]
    return left, right


def _check_reference(audit, reference):
    observed = {name: getattr(audit, name) for name in
                ("output_rows", "matched_output_rows", "unmatched_left_rows", "unmatched_right_rows")}
    for side in ("left", "right"):
        measure = getattr(audit, f"{side}_measure")
        observed[f"{side}_output_value_count"] = measure.output_value_count
        observed[f"{side}_output_sum"] = measure.output_sum
    if observed != reference:
        raise ArithmeticError("frequency audit disagrees with actual SQLite output")


def run_demo():
    left, right = example_records()
    records = []
    common = {"keys": ["customer_id"], "left_measure": "units", "right_measure": "weight"}
    for nulls in NULL_RULES:
        for how in JOIN_TYPES:
            audit = audit_join(left, right, how=how, nulls=nulls, **common)
            reference = sqlite_reference(left, right, how=how, nulls=nulls, **common)
            _check_reference(audit, reference)
            records.append({"audit": audit.to_dict(), "sqlite_reference": reference})
    guarded = audit_join(left, right, how="left", relationship="many-to-one",
                         max_output_rows=7, require_left_match=True, **common)
    try:
        guarded.enforce()
    except JoinContractError:
        pass
    else:
        raise ArithmeticError("invalid small-example contract was not blocked")
    hot = audit_join(
        ({"key": "hot", "units": 1} for _ in range(20_000)),
        ({"key": "hot", "units": 2} for _ in range(15_000)),
        keys=["key"], left_measure="units", right_measure="units", max_output_rows=1_000_000,
    )
    if hot.output_rows != 300_000_000 or hot.ok:
        raise ArithmeticError("hot-key exact count or guard failed")
    return {
        "data": "Deterministic synthetic records, not real customers, transactions, or identifiers.",
        "small_inputs": {"left": left, "right": right}, "small_joins": records,
        "guarded_small_join": guarded.to_dict(),
        "hot_key_example": {
            "audit": hot.to_dict(), "input_rows_consumed": 35_000,
            "frequency_group_entries": 2, "materialized_join_rows": 0,
            "scope": "Counts, not measured runtime or memory benchmarks; SQLite is not run on this case.",
        },
    }


def write_outputs(result, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(json.dumps(result, indent=2, ensure_ascii=False,
                                                   allow_nan=False) + "\n", encoding="utf-8")
    for side in ("left", "right"):
        (output / f"{side}.json").write_text(json.dumps(result["small_inputs"][side], indent=2) + "\n",
                                            encoding="utf-8")
    reports = [r["audit"] for r in result["small_joins"]]
    fig, axes = plt.subplots(1, 2, figsize=(11.1, 4.7), layout="constrained")
    for mode, offset, color, label in (("sql", -.18, "#367f78", "SQL equality"),
                                      ("null_safe", .18, "#ae6849", "Null-safe equality")):
        ordered = [next(r for r in reports if r["how"] == how and r["nulls"] == mode) for how in JOIN_TYPES]
        xs = [i + offset for i in range(4)]
        for ax, values in ((axes[0], [r["output_rows"] for r in ordered]),
                           (axes[1], [r["left_measure"]["output_sum"] for r in ordered])):
            bars = ax.bar(xs, values, width=.34, color=color, label=label)
            ax.bar_label(bars, padding=3, fontsize=9)
    axes[0].set(ylabel="Joined rows", title="Same inputs, explicit matching semantics", ylim=(0, 16))
    axes[1].axhline(49, color="#555555", linestyle="--", linewidth=1, label="Original left sum: 49")
    axes[1].set(ylabel="Sum of copied left units", title="Fanout changes contribution weights", ylim=(0, 112))
    for ax in axes:
        ax.set(xticks=range(4), xticklabels=JOIN_TYPES)
        ax.spines[["top", "right"]].set_visible(False)
        ax.legend(fontsize=8, loc="upper left")
        ax.grid(axis="y", alpha=.15)
    fig.savefig(output / "join-semantics.png", dpi=170)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("results"))
    args = parser.parse_args()
    result = run_demo()
    write_outputs(result, args.output)
    print(json.dumps({
        "small_joins_checked": len(result["small_joins"]),
        "row_counts": {f"{r['audit']['nulls']}/{r['audit']['how']}": r["audit"]["output_rows"]
                       for r in result["small_joins"]},
        "guard_violations": result["guarded_small_join"]["violations"],
        "hot_key_output_rows": result["hot_key_example"]["audit"]["output_rows"],
    }, indent=2))


if __name__ == "__main__":
    main()

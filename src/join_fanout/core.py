"""Frequency-table audit of one equijoin; never constructs the joined rows.

Keys use strict per-component types across both inputs. None alone represents
NULL; equality can be SQL-style or explicitly null-safe. Measures are nullable
signed int64 values, but counts and computed sums use arbitrary-precision ints.
"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass

INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1
JOIN_TYPES = ("inner", "left", "right", "full")
NULL_RULES = ("sql", "null_safe")
RELATIONSHIPS = ("one-to-one", "one-to-many", "many-to-one", "many-to-many")


def _nonnegative_int(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative built-in integer")
    return value


def _int64(value, name):
    if type(value) is not int or not INT64_MIN <= value <= INT64_MAX:
        raise ValueError(f"{name} must be a signed int64 integer or None")
    return value


def _utf8(value, name):
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8 text") from exc


@dataclass
class _Group:
    rows: int = 0
    values: int = 0
    total: int = 0


@dataclass(frozen=True)
class MeasureAudit:
    field: str | None
    input_value_count: int
    input_sum: int | None
    output_value_count: int
    output_sum: int | None


@dataclass(frozen=True)
class KeyContribution:
    key: tuple
    left_rows: int
    right_rows: int
    matched_output_rows: int
    output_rows: int
    left_copy_factor: int
    right_copy_factor: int


class JoinContractError(ValueError):
    """A complete audit was computed, but an explicit contract did not hold."""


@dataclass(frozen=True)
class Audit:
    keys: tuple[str, ...]
    how: str
    nulls: str
    relationship: str
    key_types: tuple[str | None, ...]
    left_rows: int
    right_rows: int
    left_distinct_keys: int
    right_distinct_keys: int
    matched_output_rows: int
    output_rows: int
    unmatched_left_rows: int
    unmatched_right_rows: int
    left_null_key_rows: int
    right_null_key_rows: int
    duplicate_left_matchable_keys: int
    duplicate_right_matchable_keys: int
    left_measure: MeasureAudit
    right_measure: MeasureAudit
    top_contributors: tuple[KeyContribution, ...]
    max_output_rows: int | None
    violations: tuple[str, ...]

    @property
    def ok(self):
        return not self.violations

    def enforce(self):
        """Raise before the caller executes its join; does not execute or lock it."""
        if self.violations:
            raise JoinContractError("; ".join(self.violations))
        return self

    def to_dict(self):
        return {**asdict(self), "ok": self.ok}


def _scan(records, keys, measure, types, side):
    groups = {}
    null_rows = 0
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise ValueError(f"{side} row {index} must be a mapping")
        values = []
        for position, name in enumerate(keys):
            if name not in record:
                raise ValueError(f"{side} row {index} is missing key field {name!r}")
            value = record[name]
            if value is not None:
                if type(value) not in (str, int):
                    raise ValueError(f"{side} key field {name!r} must be str, int64, or None")
                if type(value) is int:
                    _int64(value, name)
                else:
                    _utf8(value, name)
                if types[position] is None:
                    types[position] = type(value)
                elif types[position] is not type(value):
                    raise ValueError(f"key field {name!r} has mixed non-null types")
            values.append(value)
        key = tuple(values)
        null_rows += int(None in key)
        group = groups.setdefault(key, _Group())
        group.rows += 1
        if measure is not None:
            if measure not in record:
                raise ValueError(f"{side} row {index} is missing measure field {measure!r}")
            value = record[measure]
            if value is not None:
                group.total += _int64(value, measure)
                group.values += 1
    return groups, null_rows


def _key_order(key):
    # Each component has one non-null type across both inputs.
    return tuple((0, 0) if value is None else (1, value) for value in key)


def audit_join(left, right, *, keys, how="inner", nulls="sql",
               relationship="many-to-many", left_measure=None, right_measure=None,
               max_output_rows=None, require_left_match=False, require_right_match=False, top_k=5):
    """Audit a single equality join under bag semantics.

    Relationship uniqueness covers every matchable key on each required side,
    even when absent on the other side. Under SQL equality, keys containing
    None are unmatchable and excluded from that uniqueness contract.
    Inputs are consumed once; memory grows with distinct keys, not joined rows.
    """
    if isinstance(keys, (str, bytes)):
        raise ValueError("keys must be a sequence of field names, not a string")
    try:
        keys = tuple(keys)
    except TypeError as exc:
        raise ValueError("keys must be a sequence of field names") from exc
    if not keys or any(type(key) is not str or not key for key in keys) or len(set(keys)) != len(keys):
        raise ValueError("keys must contain distinct nonempty field names")
    for key in keys:
        _utf8(key, "key field name")
    if how not in JOIN_TYPES or nulls not in NULL_RULES or relationship not in RELATIONSHIPS:
        raise ValueError("unsupported join type, null rule, or relationship")
    for name, field in (("left_measure", left_measure), ("right_measure", right_measure)):
        if field is not None and (type(field) is not str or not field):
            raise ValueError(f"{name} must be a nonempty field name or None")
        if field is not None:
            _utf8(field, name)
    for flag in (require_left_match, require_right_match):
        if type(flag) is not bool:
            raise ValueError("match requirements must be booleans")
    top_k = _nonnegative_int(top_k, "top_k")
    if max_output_rows is not None:
        max_output_rows = _nonnegative_int(max_output_rows, "max_output_rows")
    types = [None] * len(keys)
    left_groups, left_nulls = _scan(left, keys, left_measure, types, "left")
    right_groups, right_nulls = _scan(right, keys, right_measure, types, "right")
    matchable = lambda key: nulls == "null_safe" or None not in key
    duplicate_left = sum(group.rows > 1 and matchable(key) for key, group in left_groups.items())
    duplicate_right = sum(group.rows > 1 and matchable(key) for key, group in right_groups.items())
    matched_rows = output_rows = unmatched_left = unmatched_right = 0
    left_count = right_count = left_sum = right_sum = 0
    contributions = []
    empty = _Group()
    for key in left_groups.keys() | right_groups.keys():
        l, r = left_groups.get(key, empty), right_groups.get(key, empty)
        paired = bool(l.rows and r.rows and matchable(key))
        matched = l.rows * r.rows if paired else 0
        if paired:
            lf, rf = r.rows, l.rows
            emitted = matched
        else:
            unmatched_left += l.rows
            unmatched_right += r.rows
            lf = int(bool(l.rows) and how in ("left", "full"))
            rf = int(bool(r.rows) and how in ("right", "full"))
            emitted = l.rows * lf + r.rows * rf
        matched_rows += matched
        output_rows += emitted
        left_count += l.values * lf
        right_count += r.values * rf
        left_sum += l.total * lf
        right_sum += r.total * rf
        if emitted:
            contributions.append(KeyContribution(key, l.rows, r.rows, matched, emitted, lf, rf))
    contributions.sort(key=lambda item: (-item.output_rows, _key_order(item.key)))
    violations = []
    if relationship in ("one-to-one", "one-to-many") and duplicate_left:
        violations.append(f"left side has {duplicate_left} duplicated matchable keys")
    if relationship in ("one-to-one", "many-to-one") and duplicate_right:
        violations.append(f"right side has {duplicate_right} duplicated matchable keys")
    if max_output_rows is not None and output_rows > max_output_rows:
        violations.append(f"output row count {output_rows} exceeds budget {max_output_rows}")
    if require_left_match and unmatched_left:
        violations.append(f"{unmatched_left} left rows have no match")
    if require_right_match and unmatched_right:
        violations.append(f"{unmatched_right} right rows have no match")

    def measure_audit(field, groups, count, total):
        original_count = sum(g.values for g in groups.values())
        original_sum = sum(g.total for g in groups.values())
        return MeasureAudit(field, original_count, original_sum if original_count else None,
                            count, total if count else None)

    return Audit(
        keys, how, nulls, relationship, tuple(t.__name__ if t else None for t in types),
        sum(g.rows for g in left_groups.values()), sum(g.rows for g in right_groups.values()),
        len(left_groups), len(right_groups), matched_rows, output_rows,
        unmatched_left, unmatched_right, left_nulls, right_nulls, duplicate_left, duplicate_right,
        measure_audit(left_measure, left_groups, left_count, left_sum),
        measure_audit(right_measure, right_groups, right_count, right_sum),
        tuple(contributions[:top_k]), max_output_rows, tuple(violations),
    )

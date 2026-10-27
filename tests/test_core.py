import json
import random

import pytest

from join_fanout import JoinContractError, audit_join
from join_fanout.core import INT64_MAX, JOIN_TYPES, NULL_RULES
from join_fanout.reference import sqlite_reference


def assert_reference(left, right, keys, how, nulls):
    options = dict(keys=keys, how=how, nulls=nulls, left_measure="value", right_measure="value")
    report = audit_join(left, right, **options)
    expected = sqlite_reference(left, right, **options)
    for field in ("output_rows", "matched_output_rows", "unmatched_left_rows", "unmatched_right_rows"):
        assert getattr(report, field) == expected[field]
    for side in ("left", "right"):
        measure = getattr(report, f"{side}_measure")
        assert measure.output_sum == expected[f"{side}_output_sum"]
        assert measure.output_value_count == expected[f"{side}_output_value_count"]
    return report


@pytest.mark.parametrize("how", JOIN_TYPES)
@pytest.mark.parametrize("nulls", NULL_RULES)
def test_composite_nulls_duplicates_and_measures_against_sqlite(how, nulls):
    left = [{"id": 1, "region": "x", "value": 10},
            {"id": 1, "region": "x", "value": -3},
            {"id": 2, "region": None, "value": 4},
            {"id": None, "region": "x", "value": None},
            {"id": 3, "region": "z", "value": 0}]
    right = [{"id": 1, "region": "x", "value": 2},
             {"id": 1, "region": "x", "value": None},
             {"id": 2, "region": None, "value": 8},
             {"id": None, "region": "x", "value": 9},
             {"id": 4, "region": "z", "value": 12}]
    assert_reference(left, right, ["id", "region"], how, nulls)


@pytest.mark.parametrize("seed", range(12))
def test_small_random_inputs_against_all_sqlite_join_modes(seed):
    rng = random.Random(seed)
    def records(count):
        return [{"id": rng.choice([None, 1, 2, 3]), "region": rng.choice([None, "", "a", "b"]),
                 "value": rng.choice([None, -7, 0, 5, 9])} for _ in range(count)]
    left, right = records(rng.randrange(9)), records(rng.randrange(9))
    for how in JOIN_TYPES:
        for nulls in NULL_RULES:
            assert_reference(left, right, ["id", "region"], how, nulls)


@pytest.mark.parametrize("left,right", [([], []), ([], [{"id": 1, "value": 3}]),
                                      ([{"id": None, "value": None}], [])])
def test_empty_inputs_and_all_null_sums(left, right):
    for how in JOIN_TYPES:
        for nulls in NULL_RULES:
            assert_reference(left, right, ["id"], how, nulls)


def test_sql_nulls_are_unmatched_not_one_group_of_matches():
    left = [{"id": None, "value": 1}] * 2
    right = [{"id": None, "value": 2}] * 3
    sql = audit_join(left, right, keys=["id"], how="full", nulls="sql", relationship="one-to-one")
    safe = audit_join(left, right, keys=["id"], how="full", nulls="null_safe", relationship="one-to-one")
    assert sql.output_rows == 5 and sql.matched_output_rows == 0 and sql.ok
    assert safe.output_rows == 6 and safe.matched_output_rows == 6 and not safe.ok
    assert sql.left_null_key_rows == 2 and sql.right_null_key_rows == 3
    assert safe.duplicate_left_matchable_keys == safe.duplicate_right_matchable_keys == 1


def test_matched_key_product_and_measure_copy_factors():
    left = [{"id": "A", "value": v} for v in (10, 20)]
    right = [{"id": "A", "value": v} for v in (1, 2, 3)]
    report = assert_reference(left, right, ["id"], "inner", "sql")
    assert report.output_rows == 6
    assert report.left_measure.input_sum == 30
    assert report.left_measure.output_sum == 90
    assert report.right_measure.input_sum == 6
    assert report.right_measure.output_sum == 12
    top = report.top_contributors[0]
    assert (top.left_copy_factor, top.right_copy_factor) == (3, 2)


def test_uniqueness_checks_unmatched_nonnull_keys_too():
    report = audit_join([{"id": 1}], [{"id": 2}, {"id": 2}], keys=["id"], relationship="many-to-one")
    assert report.output_rows == 0
    assert report.duplicate_right_matchable_keys == 1 and not report.ok
    with pytest.raises(JoinContractError, match="right side"):
        report.enforce()


@pytest.mark.parametrize("relationship,ok", [
    ("one-to-one", False), ("one-to-many", False),
    ("many-to-one", True), ("many-to-many", True),
])
def test_relationship_direction(relationship, ok):
    report = audit_join([{"id": 1}] * 2, [{"id": 1}], keys=["id"], relationship=relationship)
    assert report.ok is ok


def test_row_budget_boundary_and_coverage_are_separate_contracts():
    left, right = [{"id": 1}, {"id": 2}], [{"id": 1}, {"id": 1}, {"id": 3}]
    report = audit_join(left, right, keys=["id"], how="left", max_output_rows=3)
    assert report.enforce() is report
    blocked = audit_join(left, right, keys=["id"], how="left", max_output_rows=2,
                         require_left_match=True, require_right_match=True)
    assert len(blocked.violations) == 3
    assert blocked.unmatched_left_rows == blocked.unmatched_right_rows == 1


def test_negative_and_zero_sums_are_not_treated_as_missing():
    report = audit_join([{"id": 1, "value": -4}, {"id": 1, "value": 4}],
                        [{"id": 1}], keys=["id"], left_measure="value")
    assert report.left_measure.output_sum == 0
    assert report.left_measure.output_value_count == 2
    assert report.right_measure.field is None
    assert report.right_measure.output_sum is None


def test_aggregate_sums_do_not_overflow_int64():
    report = assert_reference([{"id": 1, "value": INT64_MAX}], [{"id": 1, "value": 0}] * 3,
                              ["id"], "inner", "sql")
    assert report.left_measure.output_sum == 3 * INT64_MAX


def test_inputs_are_consumed_once_without_join_materialization():
    class Once:
        def __init__(self, count):
            self.count, self.used = count, False
        def __iter__(self):
            assert not self.used
            self.used = True
            yield from ({"id": 7, "value": 1} for _ in range(self.count))
    report = audit_join(Once(10000), Once(10000), keys=["id"], left_measure="value",
                        max_output_rows=1000)
    assert report.output_rows == 100_000_000
    assert report.left_distinct_keys == report.right_distinct_keys == 1
    assert report.left_measure.output_sum == 100_000_000
    assert not report.ok


def test_stable_top_order_is_independent_of_input_and_hash_order():
    left = [{"id": "b"}, {"id": None}, {"id": "a"}]
    right = [{"id": "a"}, {"id": "b"}]
    a = audit_join(left, right, keys=["id"], how="full", top_k=3)
    b = audit_join(list(reversed(left)), list(reversed(right)), keys=["id"], how="full", top_k=3)
    assert a == b
    assert [r.key for r in a.top_contributors] == [(None,), ("a",), ("b",)]
    assert audit_join(left, right, keys=["id"], top_k=0).top_contributors == ()
    assert json.loads(json.dumps(a.to_dict()))["ok"] is True


def test_string_keys_preserve_case_unicode_and_empty_strings():
    left = [{"id": v, "value": 1} for v in ("a", "A", "", "é", "e\u0301")]
    right = [{"id": v, "value": 2} for v in ("a", "", "é")]
    report = assert_reference(left, right, ["id"], "full", "sql")
    assert report.matched_output_rows == 3 and report.output_rows == 5


@pytest.mark.parametrize("keys", [[], "id", ["id", "id"], [""], [True], None])
def test_bad_key_specification(keys):
    with pytest.raises(ValueError):
        audit_join([], [], keys=keys)


@pytest.mark.parametrize("rows", [[{}], [1], [{"id": 1.0}], [{"id": True}], [{"id": float("nan")}],
                                [{"id": []}], [{"id": 2**63}], [{"id": 1}, {"id": "1"}],
                                [{"id": "\ud800"}]])
def test_bad_record_keys(rows):
    with pytest.raises(ValueError):
        audit_join(rows, [], keys=["id"])


def test_cross_side_type_mismatch_is_rejected_not_silently_coerced():
    with pytest.raises(ValueError, match="mixed"):
        audit_join([{"id": 1}], [{"id": "1"}], keys=["id"])


@pytest.mark.parametrize("rows", [[{"id": 1}], [{"id": 1, "value": True}],
                                [{"id": 1, "value": 1.5}], [{"id": 1, "value": 2**63}]])
def test_bad_measures(rows):
    with pytest.raises(ValueError):
        audit_join(rows, [], keys=["id"], left_measure="value")


@pytest.mark.parametrize("options", [
    {"how": "cross"}, {"nulls": "guess"}, {"relationship": "unique"},
    {"max_output_rows": -1}, {"max_output_rows": True}, {"top_k": -1}, {"top_k": 1.5},
    {"require_left_match": 1}, {"require_right_match": "yes"},
    {"left_measure": ""}, {"right_measure": 1},
])
def test_bad_options(options):
    with pytest.raises(ValueError):
        audit_join([], [], keys=["id"], **options)


def test_sqlite_reference_refuses_large_materialization():
    with pytest.raises(ValueError, match="small"):
        sqlite_reference([{"id": 1}] * 500, [{"id": 1}] * 500, keys=["id"], how="inner", nulls="sql")

import json
import os
import subprocess
import sys

import pytest

from join_fanout.demo import example_records, run_demo, write_outputs


def test_demo_expected_counts_and_contract():
    result = run_demo()
    expected = {
        ("sql", "inner"): (5, 67, 9), ("sql", "left"): (8, 79, 9),
        ("sql", "right"): (9, 67, 31), ("sql", "full"): (12, 79, 31),
        ("null_safe", "inner"): (11, 88, 45), ("null_safe", "left"): (12, 93, 45),
        ("null_safe", "right"): (12, 88, 49), ("null_safe", "full"): (13, 93, 49),
    }
    for record in result["small_joins"]:
        audit = record["audit"]
        assert (audit["output_rows"], audit["left_measure"]["output_sum"],
                audit["right_measure"]["output_sum"]) == expected[(audit["nulls"], audit["how"])]
    assert len(result["guarded_small_join"]["violations"]) == 3
    hot = result["hot_key_example"]
    assert hot["input_rows_consumed"] == 35000
    assert hot["frequency_group_entries"] == 2
    assert hot["materialized_join_rows"] == 0
    assert hot["audit"]["left_measure"]["output_sum"] == 300_000_000
    assert hot["audit"]["right_measure"]["output_sum"] == 600_000_000


def test_demo_reproducible_outputs(tmp_path):
    result = run_demo()
    assert run_demo() == result
    write_outputs(result, tmp_path)
    assert json.loads((tmp_path / "summary.json").read_text()) == json.loads(json.dumps(result))
    assert (tmp_path / "join-semantics.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    left, right = example_records()
    assert json.loads((tmp_path / "left.json").read_text()) == left
    assert json.loads((tmp_path / "right.json").read_text()) == right


@pytest.fixture
def inputs(tmp_path):
    left, right = example_records()
    for name, records in (("left", left), ("right", right)):
        (tmp_path / f"{name}.json").write_text(json.dumps(records))
    return tmp_path / "left.json", tmp_path / "right.json"


def invoke(left, right, *arguments, env=None):
    return subprocess.run([sys.executable, "-m", "join_fanout.cli", str(left), str(right),
                           "--keys", "customer_id", *arguments],
                          text=True, capture_output=True, env=env)


def test_cli_success_and_failure_leave_inputs_unchanged(inputs):
    left, right = inputs
    before = left.read_bytes(), right.read_bytes()
    good = invoke(left, right, "--how", "left", "--left-measure", "units", "--max-output-rows", "8")
    assert good.returncode == 0 and json.loads(good.stdout)["ok"]
    bad = invoke(left, right, "--how", "left", "--relationship", "many-to-one", "--max-output-rows", "7")
    assert bad.returncode == 2
    assert not json.loads(bad.stdout)["ok"]
    assert "contract failed" in bad.stderr
    assert before == (left.read_bytes(), right.read_bytes())


@pytest.mark.parametrize("content", ['[{"customer_id": 1, "customer_id": 2}]',
                                   '[{"customer_id": NaN}]', '{"customer_id": 1}', 'not json'])
def test_cli_rejects_ambiguous_or_invalid_json(inputs, content):
    left, right = inputs
    left.write_text(content)
    process = invoke(left, right)
    assert process.returncode == 2
    assert process.stdout == ""


def test_cli_is_deterministic_across_hash_seeds(inputs):
    outputs = []
    for seed in ("1", "77"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        process = invoke(*inputs, "--how", "full", env=env)
        assert process.returncode == 0
        outputs.append(process.stdout)
    assert outputs[0] == outputs[1]


def test_demo_cli(tmp_path):
    result = subprocess.run([sys.executable, "-m", "join_fanout.demo", "--output", str(tmp_path)],
                            text=True, capture_output=True, check=True)
    assert json.loads(result.stdout)["small_joins_checked"] == 8

"""Independent SQLite materialization oracle for SMALL test/example inputs."""

from contextlib import closing
import sqlite3


def sqlite_reference(left, right, *, keys, how, nulls, left_measure=None, right_measure=None):
    """Materialize a bounded test join using SQLite, not frequency-table algebra."""
    if sqlite3.sqlite_version_info < (3, 39, 0):
        raise RuntimeError("reference checks require SQLite >= 3.39 for right/full joins")
    if len(left) * len(right) + len(left) + len(right) > 100_000:
        raise ValueError("SQLite reference is deliberately restricted to small test inputs")
    kinds = {"inner": "INNER", "left": "LEFT", "right": "RIGHT", "full": "FULL"}
    operator = {"sql": "=", "null_safe": "IS"}[nulls]
    columns = [f"k{i}" for i in range(len(keys))]
    condition = " AND ".join(f"l.{col} {operator} r.{col}" for col in columns)
    with closing(sqlite3.connect(":memory:")) as db:
        for table, records, measure in (("l", left, left_measure), ("r", right, right_measure)):
            db.execute(f"CREATE TABLE {table} (row_number INTEGER PRIMARY KEY, {', '.join(columns)}, amount INTEGER)")
            rows = [(index, *[row[k] for k in keys], row[measure] if measure else None)
                    for index, row in enumerate(records)]
            db.executemany(f"INSERT INTO {table} VALUES ({','.join(['?'] * (len(keys) + 2))})", rows)
        joined = db.execute(f"SELECT l.row_number, r.row_number, l.amount, r.amount "
                            f"FROM l {kinds[how]} JOIN r ON {condition}").fetchall()
        # Sum individual SQL output values in Python, retaining exact integers
        # even if an SQLite SUM accumulator would overflow int64.
        lvalues = [row[2] for row in joined if row[2] is not None]
        rvalues = [row[3] for row in joined if row[3] is not None]
        missing_l = db.execute(f"SELECT COUNT(*) FROM l WHERE NOT EXISTS (SELECT 1 FROM r WHERE {condition})").fetchone()[0]
        missing_r = db.execute(f"SELECT COUNT(*) FROM r WHERE NOT EXISTS (SELECT 1 FROM l WHERE {condition})").fetchone()[0]
    return {
        "output_rows": len(joined),
        "matched_output_rows": sum(row[0] is not None and row[1] is not None for row in joined),
        "unmatched_left_rows": missing_l, "unmatched_right_rows": missing_r,
        "left_output_value_count": len(lvalues), "right_output_value_count": len(rvalues),
        "left_output_sum": sum(lvalues) if lvalues else None,
        "right_output_sum": sum(rvalues) if rvalues else None,
    }

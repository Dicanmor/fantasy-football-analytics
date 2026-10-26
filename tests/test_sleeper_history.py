import polars as pl

from ff.ingest.sleeper import SCHEMA
from ff.ingest.sleeper_history import HISTORY_SCHEMA, crosscheck, read_history, save_history, update_history


def snap(*rows):
    return pl.DataFrame([dict(zip(["sleeper_id", "sl_status", "sl_part", "sl_notes", "sl_news"], r)) for r in rows], schema=SCHEMA)


def test_only_changes_are_recorded_and_clears_are_logged():
    empty = pl.DataFrame(schema=HISTORY_SCHEMA)
    h, n = update_history(empty, snap(("1", "Questionable", "Foot", None, 1), ("2", "IR", "Knee - ACL", "Surgery", 2)), "2026-10-20")
    assert n == 2
    h, n = update_history(h, snap(("1", "Questionable", "Foot", None, 9), ("2", "IR", "Knee - ACL", "Surgery", 2)), "2026-10-21")
    assert n == 0  # same label (only the news timestamp moved)
    h, n = update_history(h, snap(("1", "Out", "Foot", None, 10), ("2", "IR", "Knee - ACL", "Surgery", 2)), "2026-10-22")
    assert n == 1 and h.filter(pl.col("observed") == "2026-10-22")["sl_status"].to_list() == ["Out"]
    h, n = update_history(h, snap(("2", "IR", "Knee - ACL", "Surgery", 2)), "2026-10-23")  # player 1 cleared
    last = h.filter(pl.col("observed") == "2026-10-23")
    assert n == 1 and last["sleeper_id"].to_list() == ["1"] and last["sl_status"].to_list() == [None]
    h, n = update_history(h, snap(("2", "IR", "Knee - ACL", "Surgery", 2)), "2026-10-24")
    assert n == 0  # a cleared player is not re-cleared every day


def test_roundtrip_csv(tmp_path):
    h, _ = update_history(pl.DataFrame(schema=HISTORY_SCHEMA), snap(("1", "Out", None, None, None)), "2026-10-20")
    save_history(h, tmp_path / "h" / "x.csv")
    back = read_history(tmp_path / "h" / "x.csv")
    assert back.rows() == h.rows()


def test_crosscheck_labels():
    sl = snap(("a", "Questionable", None, None, None), ("b", "Out", None, None, None), ("c", "IR", None, None, None))
    nfl = pl.DataFrame({"gsis_id": ["ga", "gb", "gd"], "report_status": ["Questionable", "Doubtful", "Out"], "report_primary_injury": [None, None, None]})
    ids = pl.DataFrame({"gsis_id": ["ga", "gb", "gc", "gd"], "sleeper_id": ["a", "b", "c", "d"]})
    res = crosscheck(sl, nfl, ids)
    out = dict(zip(res["gsis_id"].to_list(), res["agreement"].to_list()))
    assert out == {"ga": "same", "gb": "differ", "gc": "sleeper_only", "gd": "nfl_only"}

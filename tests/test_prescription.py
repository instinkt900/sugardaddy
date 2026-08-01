"""Tests for [prescription] loading and the basal-adherence maths.

No test framework is required: run directly with

    python tests/test_prescription.py

Each check is a plain ``assert``; the file is also import-safe for pytest if it
is ever added. Timestamps are fixed epoch seconds so nothing depends on the wall
clock or the machine's local zone.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sugardaddy import analysis  # noqa: E402
from sugardaddy.config import ConfigError, PrescriptionConfig, load_config  # noqa: E402
from sugardaddy.models import InsulinDose  # noqa: E402

MELB = ZoneInfo("Australia/Melbourne")

# 2026-07-27 00:00:00 +10:00 (Melbourne) as an anchor for building fixtures.
DAY0 = 1_785_074_400
DAY = 86_400

BASE_TOML = """
[database]
path = "/tmp/x.db"

[web]
timezone = "Australia/Melbourne"
"""


def _load(extra: str):
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
        fh.write(BASE_TOML + extra)
        path = fh.name
    try:
        return load_config(path)
    finally:
        Path(path).unlink()


def basal(day_offset: int, hour: int, units: float = 36.0) -> InsulinDose:
    """A basal dose at a local hour on DAY0 + day_offset."""
    return InsulinDose(ts_utc=DAY0 + day_offset * DAY + hour * 3600, units=units, kind="basal")


def test_empty_prescription_is_absent_not_zero():
    cfg = _load("")
    assert cfg.prescription.configured is False
    assert cfg.prescription.basal_units is None


def test_prescription_loads():
    cfg = _load(
        '\n[prescription]\nreviewed = "2026-03-14"\nbasal_product = "Toujeo"\n'
        "basal_units = 36\n"
    )
    assert cfg.prescription.configured is True
    assert cfg.prescription.basal_units == 36
    assert cfg.prescription.basal_product == "Toujeo"


def test_prescription_requires_a_review_date():
    # A value without its date must fail at load: an undated prescription in a
    # report has already misled whoever read it.
    try:
        _load('\n[prescription]\nbasal_units = 36\n')
    except ConfigError as exc:
        assert "reviewed" in str(exc), exc
    else:
        raise AssertionError("expected ConfigError for an undated prescription")


def test_prescription_rejects_bad_date_and_values():
    for extra in (
        '\n[prescription]\nreviewed = "14/03/2026"\nbasal_units = 36\n',
        '\n[prescription]\nreviewed = "2026-03-14"\nbasal_units = 0\n',
        '\n[prescription]\nreviewed = "2026-03-14"\nisf = -1\n',
    ):
        try:
            _load(extra)
        except ConfigError:
            pass
        else:
            raise AssertionError(f"expected ConfigError for: {extra!r}")


def test_prescription_rejects_unknown_key():
    try:
        _load('\n[prescription]\nreviewed = "2026-03-14"\nbasal_brand = "x"\n')
    except ConfigError as exc:
        assert "basal_brand" in str(exc), exc
    else:
        raise AssertionError("expected ConfigError for an unknown key")


def test_adherence_counts_local_days():
    # One dose a day at 18:00 local, covering every whole day in the window.
    doses = [basal(1, 18), basal(2, 18), basal(3, 18)]
    out = analysis.basal_adherence(doses, DAY0, DAY0 + 4 * DAY, MELB, prescribed_units=36.0)
    assert out["dose_count"] == 3, out
    assert out["day_count"] == 5, out  # 3 whole days plus both partial edges
    assert out["whole_day_count"] == 3, out
    assert out["days_with_none"] == 0, out
    assert out["days_matching_prescribed"] == 3, out


def test_adherence_flags_a_missed_day():
    doses = [basal(1, 18), basal(3, 18)]  # nothing on day 2
    out = analysis.basal_adherence(doses, DAY0, DAY0 + 4 * DAY, MELB, prescribed_units=36.0)
    assert out["days_with_none"] == 1, out
    missed = [e for e in out["days"] if e["count"] == 0 and not e.get("partial")]
    assert len(missed) == 1, out["days"]


def test_edge_days_are_partial_not_missed():
    # No dose on either edge day. They must not count as missed, since the
    # window may simply have cut in after the usual dose time.
    doses = [basal(1, 18), basal(2, 18)]
    out = analysis.basal_adherence(doses, DAY0, DAY0 + 3 * DAY, MELB, prescribed_units=36.0)
    assert out["days"][0]["partial"] is True, out["days"]
    assert out["days"][-1]["partial"] is True, out["days"]
    assert out["days_with_none"] == 0, out


def test_double_dose_day_is_not_a_match():
    # Two doses summing to the prescribed amount is not the same as one dose of
    # it, so counting units alone would be wrong.
    doses = [basal(1, 8, 18.0), basal(1, 20, 18.0)]
    out = analysis.basal_adherence(doses, DAY0, DAY0 + 3 * DAY, MELB, prescribed_units=36.0)
    day1 = [e for e in out["days"] if e["day"] == "2026-07-28"][0]
    assert day1["count"] == 2, day1
    assert day1["units"] == 36.0, day1
    assert day1["matches_prescribed"] is False, day1
    assert out["days_with_multiple"] == 1, out


def test_adherence_without_a_prescription():
    # Day counts still stand; the comparison fields go None rather than the
    # function inventing an expected dose.
    doses = [basal(1, 18), basal(2, 18)]
    out = analysis.basal_adherence(doses, DAY0, DAY0 + 4 * DAY, MELB)
    assert out["prescribed_units"] is None, out
    assert out["days_matching_prescribed"] is None, out
    assert all(e["matches_prescribed"] is None for e in out["days"]), out["days"]
    assert out["dose_count"] == 2, out


def test_adherence_ignores_non_basal():
    doses = [
        basal(1, 18),
        InsulinDose(ts_utc=DAY0 + DAY + 3600, units=6.0, kind="bolus"),
        InsulinDose(ts_utc=DAY0 + DAY + 7200, units=3.0, kind="correction"),
    ]
    out = analysis.basal_adherence(doses, DAY0, DAY0 + 3 * DAY, MELB, prescribed_units=36.0)
    assert out["dose_count"] == 1, out
    assert out["total_units"] == 36.0, out


def test_local_day_boundary():
    # 23:50 and 00:10 either side of local midnight are different days, even
    # though they are 20 minutes apart.
    doses = [basal(1, 23, 36.0), basal(1, 24, 36.0)]
    out = analysis.basal_adherence(doses, DAY0, DAY0 + 4 * DAY, MELB, prescribed_units=36.0)
    counted = {e["day"]: e["count"] for e in out["days"]}
    assert counted["2026-07-28"] == 1, counted
    assert counted["2026-07-29"] == 1, counted


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} checks passed")


if __name__ == "__main__":
    _run_all()

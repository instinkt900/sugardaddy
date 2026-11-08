"""Tests for the phone's quick-note buttons.

No test framework is required: run directly with

    python tests/test_quick_notes.py

Quick notes are short texts the phone's Note sheet offers as one-tap buttons
(a tap fills the box; logging is still a separate tap). They are set on the
desktop's Phone view and stored server-side; until someone sets them they are
the notes written more than once lately (`analysis.note_presets`).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from sugardaddy.analysis import note_presets  # noqa: E402
from sugardaddy.models import Note  # noqa: E402
from sugardaddy.web import create_app  # noqa: E402

T0 = 1_784_764_800  # 2026-07-23 00:00:00 UTC


def fresh_client() -> TestClient:
    tmp = Path(tempfile.mkdtemp(prefix="sugardaddy-test-"))
    cfg = tmp / "config.toml"
    cfg.write_text(f'[database]\npath = "{tmp / "t.db"}"\n[web]\ntimezone = "UTC"\nunits = "mmol/L"\n')
    return TestClient(create_app(str(cfg), start_ingest=False))


def notes(*texts: str) -> list[Note]:
    return [Note(ts_utc=T0 + i * 60, text=t) for i, t in enumerate(texts)]


# --- learned defaults (pure) ------------------------------------------------

def test_only_repeated_notes_are_learned():
    got = note_presets(notes("run", "felt sick", "Run", "new sensor", "new sensor", "run"))
    assert got == ["run", "new sensor"], got


def test_learned_matches_ignore_case_and_spacing():
    got = note_presets(notes("Claratyne", "claratyne ", "  CLARATYNE"))
    assert len(got) == 1 and got[0].lower() == "claratyne", got


def test_learned_spelling_is_the_most_used_latest_on_tie():
    assert note_presets(notes("run", "Run", "Run")) == ["Run"]
    assert note_presets(notes("run", "Run")) == ["Run"]


def test_learned_ties_go_to_the_most_recent():
    assert note_presets(notes("a", "a", "b", "b")) == ["b", "a"]


def test_nothing_learned_from_an_empty_log():
    assert note_presets([]) == []
    assert note_presets(notes("", "   ")) == []


# --- the routes -------------------------------------------------------------

def test_set_by_hand_cleans_and_keeps_order():
    c = fresh_client()
    r = c.put("/api/settings/note-presets", json={"presets": ["New sensor", "  run  ", "RUN", "", "Claratyne"]})
    assert r.status_code == 200, r.text
    assert r.json()["presets"] == ["New sensor", "run", "Claratyne"]
    assert r.json()["custom"] is True
    assert c.get("/api/shortcuts").json()["notes"] == ["New sensor", "run", "Claratyne"]


def test_an_empty_list_is_a_choice_not_a_reset():
    c = fresh_client()
    for _ in range(2):
        c.post("/api/note", data={"text": "run", "ts": ""})
    assert c.get("/api/shortcuts").json()["notes"] == ["run"]
    c.put("/api/settings/note-presets", json={"presets": []})
    assert c.get("/api/shortcuts").json()["notes"] == []
    r = c.put("/api/settings/note-presets", json={"presets": None})
    assert r.json()["custom"] is False and r.json()["presets"] == ["run"]


def test_bad_shape_is_refused():
    c = fresh_client()
    r = c.put("/api/settings/note-presets", json={"presets": "run"})
    assert r.status_code == 400


def test_long_texts_are_clipped_and_the_list_capped():
    c = fresh_client()
    r = c.put("/api/settings/note-presets", json={"presets": ["x" * 200] + [f"n{i}" for i in range(20)]})
    got = r.json()["presets"]
    assert len(got) == 12 and len(got[0]) == 80, got


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"ok  {name}")
    print(f"\n{n} checks passed")

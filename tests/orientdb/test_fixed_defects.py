from tests.orientdb.helpers import GeneratorConfig, build
from ganda.orientdb import known_bugs
from ganda.orientdb.rules import BASELINE
from ganda.core.query.generators.registry import all_cases


def _marked_cases():
    """A few generated cases that carry at least one `known_bugs` entry each."""
    out = []
    for seed in range(1, 60):
        schema, inst, rng = build(seed, GeneratorConfig(index_probability=0.9))
        for c in all_cases(rng, schema, inst):
            if c.known_bugs:
                out.append(c)
        if len(out) >= 8:
            break
    assert out, "no case was marked by any guard in 60 seeds"
    return out


def test_discard_fixed_returns_none_for_fixed_ids():
    known_bugs._FIXED = frozenset({"O13", "O16"})
    try:
        assert known_bugs.discard_fixed("O13") is None
        assert known_bugs.discard_fixed("O16") is None
        assert known_bugs.discard_fixed("O7") == "O7"
        assert known_bugs.discard_fixed(None) is None
    finally:
        known_bugs._FIXED = frozenset()


def test_activate_leaves_set_empty_on_version_mismatch():
    known_bugs.activate_fixed_defects("not-the-baseline-build-9.9.9")
    assert known_bugs.fixed_defects() == frozenset()


def test_activate_leaves_set_empty_on_unknown_version():
    known_bugs.activate_fixed_defects(None)
    assert known_bugs.fixed_defects() == frozenset()


def test_activate_reads_fixed_from_baseline_on_version_match(tmp_path, monkeypatch):
    """A baseline that lists `O13` as fixed on `9.9.9-test` activates on a server of that
    build, and `discard_fixed('O13')` returns None (the guard's hit no longer withholds)."""
    import json
    import pathlib

    sentinel = tmp_path / "baseline.json"
    sentinel.write_text(json.dumps({"version": "9.9.9-test", "probed": "2026-11-03", "held": [], "fixed": {"O13": ["Cxxx"]}}))
    original = BASELINE.path
    object.__setattr__(BASELINE, "path", sentinel)
    try:
        active = known_bugs.activate_fixed_defects("9.9.9-test")
        assert "O13" in active
        assert known_bugs.discard_fixed("O13") is None
    finally:
        object.__setattr__(BASELINE, "path", original)
        known_bugs.activate_fixed_defects(None)


def test_fixed_defect_is_dropped_from_marked_cases(tmp_path):
    """A baseline that fixes every defect currently marked on a few generated cases
    makes the guards' hits vanish for the next run of the same generator seeds."""
    import json

    before = _marked_cases()
    ids = {fid for c in before for fid in c.known_bugs.values()}
    assert ids, "generator produced no defect ids"

    sentinel = tmp_path / "baseline.json"
    sentinel.write_text(json.dumps({"version": "9.9.9-test", "probed": "2026-11-03", "held": [], "fixed": {fid: ["Cxxx"] for fid in ids}}))
    original = BASELINE.path
    object.__setattr__(BASELINE, "path", sentinel)
    try:
        known_bugs.activate_fixed_defects("9.9.9-test")
        after_ids: set = set()
        for seed in range(1, 60):
            schema, inst, rng = build(seed, GeneratorConfig(index_probability=0.9))
            for c in all_cases(rng, schema, inst):
                after_ids.update(c.known_bugs.values())
        assert ids - after_ids, f"guards still marked {ids & after_ids} after activation; expected at least one to be dropped"
    finally:
        object.__setattr__(BASELINE, "path", original)
        known_bugs.activate_fixed_defects(None)

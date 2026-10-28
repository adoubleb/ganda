"""The probe-to-line map's loader and `lines_of` (`ganda.orientdb.code_coverage.probe_lines`),
over a hand-made map -- no Docker, no JVM."""
import gzip
import json

import pytest

from ganda.orientdb.code_coverage import probe_lines
from ganda.orientdb.code_coverage.collector import ExecData

OUTER, INNER, SYNTHETIC, NO_SOURCE = 11, -22, 33, 44

CLASSES = [
    # an outer class and its inner class: one source file, line 20 in both
    {"id": OUTER, "name": "p/q/Outer", "jar": "orientdb-core", "package": "p/q", "source": "Outer.java", "analyzed": True,
     "probes": 3, "lines": [10, 11, 12, 20], "probe_lines": [[10, 11], [12, 20], []]},
    {"id": INNER, "name": "p/q/Outer$Inner", "jar": "orientdb-core", "package": "p/q", "source": "Outer.java", "analyzed": True,
     "probes": 2, "lines": [20, 21], "probe_lines": [[20], [20, 21]]},
    # skipped by the analyzer: probes, no lines, no path
    {"id": SYNTHETIC, "name": "p/q/Gen$1", "jar": "orientdb-core", "package": "p/q", "source": None, "analyzed": False,
     "probes": 1, "lines": [], "probe_lines": [[]]},
    # no SourceFile attribute: keyed by its vm name
    {"id": NO_SOURCE, "name": "p/NoSource", "jar": "orientdb-server", "package": "p", "source": None, "analyzed": True,
     "probes": 1, "lines": [5], "probe_lines": [[5]]},
]


def _write(path, classes=CLASSES, header=None) -> str:
    header = header or {"format": probe_lines.FORMAT, "jacoco": "0.8.13", "classes": len(classes), "jars": ["orientdb-core", "orientdb-server"]}
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(header) + "\n")
        for r in classes:
            fh.write(json.dumps(r) + "\n")
    return str(path)


@pytest.fixture
def pmap(tmp_path):
    return probe_lines.load(_write(tmp_path / "map.jsonl.gz"))


def test_probes_of_reads_the_set_bits_lowest_first():
    assert list(probe_lines.probes_of(0)) == []
    assert list(probe_lines.probes_of(0b1011)) == [0, 1, 3]
    assert list(probe_lines.probes_of(1 << 200 | 1 << 64)) == [64, 200]


def test_inner_classes_merge_onto_their_source_file(pmap):
    assert pmap.classes[INNER].path == "p/q/Outer.java" == pmap.classes[OUTER].path
    lines = pmap.lines_of({OUTER: 0b010, INNER: 0b11})
    assert lines == {("p/q/Outer.java", 12), ("p/q/Outer.java", 20), ("p/q/Outer.java", 21)}


def test_a_window_covers_the_union_of_its_probes_lines(pmap):
    assert pmap.lines_of({OUTER: 0b001}) == {("p/q/Outer.java", 10), ("p/q/Outer.java", 11)}
    assert pmap.lines_of({OUTER: 0b100}) == set()  # a probe that proves no line
    assert pmap.lines_of({OUTER: 0b111}) == pmap.lines_of({OUTER: 0b001}) | pmap.lines_of({OUTER: 0b010})
    assert pmap.lines_of({NO_SOURCE: 1, SYNTHETIC: 1}) == {("p/NoSource", 5)}
    assert pmap.lines_of({}) == set() and pmap.lines_of({OUTER: 0}) == set()


def test_unknown_classes_are_ignored_and_reported(pmap):
    window = {OUTER: 1, 999: 0b11, 1000: 0}
    assert pmap.lines_of(window) == pmap.lines_of({OUTER: 1})
    assert pmap.unknown(window) == {999}


def test_a_probe_past_the_class_is_another_build(pmap):
    with pytest.raises(ValueError, match="another build"):
        pmap.lines_of({INNER: 0b100})


def test_executable_lines_count_a_shared_line_once(pmap):
    executable = pmap.executable()
    assert len(executable) == 6  # 10 11 12 20 21 of Outer.java, 5 of NoSource
    assert pmap.by_package(executable) == {"p/q": 5, "p": 1}
    assert pmap.by_package(pmap.lines_of({INNER: 0b10})) == {"p/q": 2}


def test_a_window_round_trips_through_execution_data(pmap):
    window = {OUTER: 0b101, INNER: 0b10, NO_SOURCE: 1}
    back = ExecData.from_bytes(pmap.exec_data(window).to_bytes())
    assert back.masks == window
    assert back.names[INNER] == "p/q/Outer$Inner" and back.sizes[OUTER] == 3
    assert pmap.lines_of(back.masks) == pmap.lines_of(window)


def test_the_synthetic_window_is_seeded_and_inside_the_classes(pmap):
    a = probe_lines.synthetic_window(pmap, 7, 1.0, 0.5)
    assert a == probe_lines.synthetic_window(pmap, 7, 1.0, 0.5)
    assert set(a) == {OUTER, INNER, SYNTHETIC, NO_SOURCE}
    assert all(0 < m < 1 << pmap.classes[cid].probes for cid, m in a.items())
    assert probe_lines.synthetic_window(pmap, 7, 0.0, 0.5) == {}


def test_the_loader_refuses_another_format_or_a_short_file(tmp_path):
    with pytest.raises(ValueError, match="is not a"):
        probe_lines.load(_write(tmp_path / "a.jsonl.gz", header={"format": "other", "classes": 4}))
    with pytest.raises(ValueError, match="the header says 5"):
        probe_lines.load(_write(tmp_path / "b.jsonl.gz", header={"format": probe_lines.FORMAT, "classes": 5}))


def test_the_default_map_sits_beside_the_image_lib():
    assert probe_lines.default_map("/x/coverage/lib-282bbeb41447") == "/x/coverage/probe-lines-282bbeb41447.jsonl.gz"

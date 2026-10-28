"""The offline report (`ganda.core.engine.mc_report`) over hand-written window logs and a
toy line map: unit "u" element i executes line i of `parser.rs`, unit "v" element i line
i of `exec.rs`."""
import pytest

from ganda.core.engine import mc_report
from ganda.core.engine.window_log import WindowLog


class ToyMapper(mc_report.LineMapper):
    files = {"u": "src/parser.rs", "v": "src/exec.rs"}

    def unit_lines(self, unit, name, encoded):
        return frozenset((self.files[unit], i) for i in encoded)

    def executable_lines(self):
        return {"src/parser.rs": set(range(10)), "src/exec.rs": set(range(10)), "vendor/x.rs": set(range(100))}

    def merge(self, encodings):
        return sorted({e for enc in encodings for e in enc})


class Case:
    generator_name = "ir_select"
    shape = "s"


def _log(path, first_case, second_case=None):
    log = WindowLog(str(path), {"engine": "toy"})
    log.other("boot", {"u": [0]})
    log.idle({"v": [9]})
    log.case(Case(), [("doc", "graph"), ("doc", "rel")], first_case)
    log.case(Case(), [("doc", "graph")], second_case or {"doc": {"u": [5]}, "graph": {"u": [5]}})
    log.seed_done(1)
    log.close()
    return str(path)


A = {"doc": {"u": [1, 2], "v": [9]}, "graph": {"u": [1], "v": [4]}, "rel": {"u": [1, 3], "v": [4]}}
COMPS = mc_report.Components([(r"parser", "parser"), (r"exec", "executor")])


def in_scope(f):
    return f.startswith("src/")


def test_the_report_derives_every_number_from_the_log(tmp_path):
    r = mc_report.analyze(_log(tmp_path / "a.gz", A), ToyMapper(), in_scope, COMPS)
    assert r["executable_lines"] == 20 and r["covered_lines"] == 7  # u 0,1,2,3,5 and v 4,9
    assert r["line_coverage_pct"] == 35.0
    # case 1: union {u1,u2,u3,v4,v9}, every {u1} -> raw MC {u2,u3,v4,v9}; v9 is background
    assert r["mc_lines_raw"] == 4 and r["background_lines"] == 1 and r["mc_lines"] == 3
    assert r["zero_mc_cases"] == 1 and r["cases"] == 2 and r["mc_lines_in_one_case_only"] == 3
    assert r["components"]["parser"] == {"lines": 10, "covered": 5, "mc": 2, "mc_raw": 2, "background": 0, "replicate_noise": 0, "covered_pct": 50.0, "mc_of_covered_pct": 40.0, "share_of_mc_pct": 66.667}
    assert r["components"]["executor"]["background"] == 1 and r["components"]["executor"]["mc_raw"] == 2
    assert r["data_model_pairs"]["doc~graph"] == {"mc_lines": 2, "pairs": 2}
    assert "| line coverage % | 35.0 |" in mc_report.markdown(r, "toy")


def test_twins_take_out_what_the_same_variant_did_differently(tmp_path):
    b = dict(A, rel={"u": [1], "v": [4]})  # the twin's rel did not reach u3
    noise = mc_report.replicate_noise(_log(tmp_path / "a.gz", A), _log(tmp_path / "b.gz", b), ToyMapper(), in_scope)
    assert noise == {"lines": frozenset({("src/parser.rs", 3)}), "seeds": 1, "windows": 5}
    ra = mc_report.analyze(str(tmp_path / "a.gz"), ToyMapper(), in_scope, COMPS, noise=noise, keep_sets=True)
    rb = mc_report.analyze(str(tmp_path / "b.gz"), ToyMapper(), in_scope, COMPS, noise=noise, keep_sets=True)
    assert ra["mc_lines"] == 2 and ra["replicate_noise_lines"] == 1  # u2 and v4
    # before the noise: A's MC {u2,u3,v4}, B's {u2,v4}
    assert mc_report.agreement(ra, rb) == {"mc_a_before_noise": 3, "mc_b_before_noise": 2, "mc_both_before_noise": 2, "mc_jaccard_before_noise": 0.6667, "covered_jaccard": 0.8571}


def test_twins_that_ask_different_questions_are_refused(tmp_path):
    a = _log(tmp_path / "a.gz", A)
    b = _log(tmp_path / "b.gz", {"doc": {"u": [1]}, "graph": {"u": [1]}})
    with pytest.raises(ValueError, match="differs between the twins"):
        mc_report.replicate_noise(a, b, ToyMapper(), in_scope)


class SplitMapper(ToyMapper):
    """Unit "s": line 7 of exec.rs runs `c0 - c1` times (a region counted by difference)."""

    def unit_lines(self, unit, name, encoded):
        if unit != "s":
            return super().unit_lines(unit, name, encoded)
        counts = dict(map(tuple, encoded))
        return frozenset({("src/exec.rs", 7)}) if counts.get(0, 0) - counts.get(1, 0) > 0 else frozenset()

    def merge(self, encodings):
        if all(isinstance(e, int) for enc in encodings for e in enc):  # a toy unit: line numbers
            return sorted({e for enc in encodings for e in enc})
        total = {}
        for enc in encodings:
            for c, inc in enc:
                total[c] = total.get(c, 0) + inc
        return [[c, n] for c, n in total.items()]


def test_a_region_split_across_two_windows_is_left_out(tmp_path):
    # the function's entry (c0) lands in doc's window, its branch (c1) in graph's: doc
    # seems to run line 7, which the run as a whole never did
    path = _log(tmp_path / "a.gz", {"doc": {"u": [1], "s": [[0, 1]]}, "graph": {"u": [1], "s": [[1, 1]]}, "rel": {"u": [1]}})
    guard = mc_report.cumulative(path, SplitMapper(), in_scope)
    assert ("src/exec.rs", 7) not in guard
    unguarded = mc_report.analyze(path, SplitMapper(), in_scope, COMPS)
    r = mc_report.analyze(path, SplitMapper(), in_scope, COMPS, guard=guard)
    assert unguarded["mc_lines"] == 1 and r["mc_lines"] == 0 and r["split_window_lines_dropped"] == 1


def _seeds_log(path, seeds):
    """A log of several seeds: `seeds` [[case windows, ...], ...]."""
    log = WindowLog(str(path), {"engine": "toy"})
    log.idle({"v": [9]})
    for n, cases in enumerate(seeds):
        for w in cases:
            log.case(Case(), [("doc", "graph")], w)
        log.seed_done(n)
    log.close()
    return str(path)


def test_the_forked_passes_give_the_serial_numbers(tmp_path):
    seeds_a = [[{"doc": {"u": [1, 2]}, "graph": {"u": [1]}}], [{"doc": {"u": [3]}, "graph": {"u": [3, 4]}}, {"doc": {"v": [1]}, "graph": {"v": [2]}}], [{"doc": {"u": [5]}, "graph": {"u": [5]}}]]
    seeds_b = [seeds_a[0], [{"doc": {"u": [3]}, "graph": {"u": [3]}}, seeds_a[1][1]], seeds_a[2]]  # u4 is noise
    a, b = _seeds_log(tmp_path / "a.gz", seeds_a), _seeds_log(tmp_path / "b.gz", seeds_b)
    m = ToyMapper()
    ga, gb = mc_report.cumulative(a, m, in_scope), mc_report.cumulative(b, m, in_scope)
    noise = mc_report.replicate_noise(a, b, m, in_scope, ga, gb)
    serial = mc_report.analyze(a, m, in_scope, COMPS, noise=noise, keep_sets=True, guard=ga)
    for workers in (1, 2, 3):
        ra, rb = mc_report.evaluate(a, b, m, in_scope, COMPS, workers=workers)
        assert ra == serial, workers
        assert rb["seeds"] == 3 and ra["replicate_noise_lines"] == 1 and ra["mc_lines"] == 3  # u2, v1, v2

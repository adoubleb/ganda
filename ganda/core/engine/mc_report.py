"""Line coverage and line-based metamorphic coverage of a campaign, from its window log
(`ganda.core.engine.window_log`): the offline half of the evaluation.

Nothing here is measured: every number is derived from the log, the build's line map
(`LineMapper`: which source lines a window's raw elements prove executed, with the
coverage tool's own semantics) and two tables written down before the campaign ran --
the scope (which source files are the engine) and the components (which files are the
parser, the optimizer, ...). The same inputs always give the same report.

Definitions (a line is a (source file, line number) of a file in scope):

- covered: lines some window of the campaign executed (boot, setup, cases, idle: the
  log holds every element the campaign reached, so this is the cumulative profile's
  line coverage, which the coverage tool's own report must confirm);
- a case's MC: lines some but not all of its compared variants executed (0 < k < N);
  the campaign's MC is the union over its cases;
- background: lines an idle window executed; replicate noise: lines the same variant of
  the same case executed differently in two independent runs of the same seeds (twins).
  MC is reported raw, less the background, and less both (the headline).
- diagnostics, not taken out: MC lines that are MC in one case only (order effects such
  as lazy initialization show up there), and, for an A/A log (`--coverage-repeat`), the
  lines a variant executed on one of its two back-to-back runs only -- the second run
  meets warm caches (a parsed statement, a cached plan), so that difference is a property
  of the engine, not noise.
"""
from __future__ import annotations

import json
import re

from ganda.core.engine import window_log
from ganda.core.oracle.case import data_model_of


class LineMapper:
    """A build's map from a window's raw elements to source lines."""

    def unit_lines(self, unit: str, name: "str | None", encoded) -> frozenset:
        """The (file, line) a unit's elements (in the window log's encoding) executed."""
        raise NotImplementedError

    def executable_lines(self) -> dict:
        """{file: set of lines} the build can execute: the denominator."""
        raise NotImplementedError

    def merge(self, encodings: list):
        """One unit's elements over several windows, in the log's encoding (the run's
        cumulative coverage of the unit)."""
        raise NotImplementedError

    def unit_files(self, unit: str) -> "set | None":
        """The source files a unit's lines can be in (None: unknown, map it to see)."""
        return None


class Components:
    """Ordered (regular expression over the file path, component) rules; the first
    match decides, `other` when none does."""

    def __init__(self, rules: list):
        self.rules = [(re.compile(rx), name) for rx, name in rules]

    def of(self, path: str) -> str:
        for rx, name in self.rules:
            if rx.search(path):
                return name
        return "other"


def _scope(mapper: LineMapper, in_scope, cache: dict):
    """`in_scope` memoized per file, and a unit test: False for a unit none of whose
    files is in scope (it is skipped without being mapped)."""
    files: dict = {}

    def file_in(f):
        v = files.get(f)
        if v is None:
            v = files[f] = bool(in_scope(f))
        return v

    def unit_in(unit):
        v = cache.get(unit)
        if v is None:
            fs = mapper.unit_files(unit)
            v = cache[unit] = True if fs is None else any(file_in(f) for f in fs)
        return v

    return file_in, unit_in


def _lines(mapper: LineMapper, names: dict, window: dict, cache: dict, in_scope) -> frozenset:
    """The in-scope lines of a window; `cache` keeps each unit's lines per encoding and
    the scope tests (`in_scope` is called once per file)."""
    scope = cache.get(None)
    if scope is None:
        scope = cache[None] = _scope(mapper, in_scope, {})
    file_in, unit_in = scope
    out: set = set()
    for unit, enc in window.items():
        if not unit_in(unit):
            continue
        key = (unit, enc if isinstance(enc, str) else repr(enc))
        lines = cache.get(key)
        if lines is None:
            lines = frozenset(fl for fl in mapper.unit_lines(unit, names.get(unit), enc) if file_in(fl[0]))
            cache[key] = lines
        out |= lines
    return frozenset(out)


# --- reading a log in parts ---------------------------------------------------------
#
# Every pass reads the log as a whole or a PART of its seeds, (k, n): the seeds whose
# position in the log is k modulo n. The log is decompressed whole by every reader, but
# only a part's records are parsed and mapped, so n forked workers (sharing the loaded
# line map) split the work. Only complete seeds count: a seed's records are the ones
# before its seed record. Units' names are not read (no line map needs them).

_SEED = '{"t":"seed"'


def _seed_records(log_path: str, part: "tuple | None" = None):
    """(seed number, [record]) of every complete seed of a log, or of a part's seeds."""
    import gzip

    index = 0
    buf: list = []
    with gzip.open(log_path, "rt") as fh:
        try:
            for line in fh:
                if not line.endswith("\n"):
                    break
                if line.startswith(_SEED):
                    if part is None or index % part[1] == part[0]:
                        yield json.loads(line)["seed"], [json.loads(x) for x in buf]
                    index += 1
                    buf = []
                elif part is None or index % part[1] == part[0]:
                    if not line.startswith('{"t":"unit"') and not line.startswith('{"t":"meta"'):
                        buf.append(line)
        except (EOFError, OSError):
            return


def meta_of(log_path: str) -> "dict | None":
    for rec in window_log.read(log_path):
        return rec if rec["t"] == "meta" else None
    return None


# --- the passes ------------------------------------------------------------------------


def _pass_units(log_path: str, mapper: LineMapper, part) -> "tuple[dict, list]":
    """({unit: its windows merged}, [idle window]) of a part."""
    per_unit: dict = {}
    idle: list = []
    for _, recs in _seed_records(log_path, part):
        for rec in recs:
            if "w" not in rec:
                continue
            if rec["t"] == "idle":
                idle.append(rec["w"])
            windows = rec["w"].values() if rec["t"] == "case" else [rec["w"]]
            for w in windows:
                for unit, enc in w.items():
                    per_unit.setdefault(unit, []).append(enc)
    return {u: mapper.merge(encs) for u, encs in per_unit.items()}, idle


def _guard_and_background(parts: list, mapper: LineMapper, in_scope) -> "tuple[frozenset, frozenset]":
    """The run's cumulative lines (every window merged per unit, then mapped) and its
    background (the idle windows' lines within them) from the parts of `_pass_units`."""
    per_unit: dict = {}
    idle: list = []
    for units, idles in parts:
        for u, enc in units.items():
            per_unit.setdefault(u, []).append(enc)
        idle += idles
    file_in, unit_in = _scope(mapper, in_scope, {})
    guard: set = set()
    for unit, encs in per_unit.items():
        if unit_in(unit):
            guard |= {fl for fl in mapper.unit_lines(unit, None, mapper.merge(encs)) if file_in(fl[0])}
    guard = frozenset(guard)
    cache: dict = {}
    background: set = set()
    for w in idle:
        background |= _lines(mapper, {}, w, cache, in_scope) & guard
    return guard, frozenset(background)


def _pass_noise(log_a: str, log_b: str, mapper: LineMapper, in_scope, part, guard_a, guard_b) -> dict:
    noise: set = set()
    seeds = windows = 0
    cache_a: dict = {}
    cache_b: dict = {}
    for (sa, ra), (sb, rb) in zip(_seed_records(log_a, part), _seed_records(log_b, part)):
        ca = [r for r in ra if r["t"] == "case"]
        cb = [r for r in rb if r["t"] == "case"]
        if sa != sb or len(ca) != len(cb):
            raise ValueError(f"the twins diverged: seed {sa} ({len(ca)} cases) against seed {sb} ({len(cb)} cases)")
        seeds += 1
        for x, y in zip(ca, cb):
            if (x["gen"], x["shape"], sorted(x["w"])) != (y["gen"], y["shape"], sorted(y["w"])):
                raise ValueError(f"seed {sa}: a case differs between the twins ({x['gen']} {x['shape']} against {y['gen']} {y['shape']})")
            for v, cov in x["w"].items():
                la = _lines(mapper, {}, cov, cache_a, in_scope)
                lb = _lines(mapper, {}, y["w"][v], cache_b, in_scope)
                if guard_a is not None:
                    la, lb = la & guard_a, lb & guard_b
                noise |= la ^ lb
                windows += 1
    return {"lines": noise, "seeds": seeds, "windows": windows}


def _merge_noise(parts: list) -> dict:
    out = {"lines": set(), "seeds": 0, "windows": 0}
    for p in parts:
        out["lines"] |= p["lines"]
        out["seeds"] += p["seeds"]
        out["windows"] += p["windows"]
    out["lines"] = frozenset(out["lines"])
    return out


class _Acc:
    """What one pass over a part adds up; `merge` joins the parts."""

    def __init__(self):
        self.covered: set = set()
        self.compared: set = set()
        self.mc: set = set()
        self.repeat_diff: set = set()
        self.unguarded: set = set()  # every window line before the guard
        self.mc_cases: dict = {}  # MC line -> in how many cases (the single-case diagnostic)
        self.gens: dict = {}
        self.pairs: dict = {}
        self.cases = self.zero = self.repeats = self.seeds = 0

    def merge(self, o: "_Acc") -> "_Acc":
        self.covered |= o.covered
        self.compared |= o.compared
        self.mc |= o.mc
        self.repeat_diff |= o.repeat_diff
        self.unguarded |= o.unguarded
        for fl, n in o.mc_cases.items():
            self.mc_cases[fl] = self.mc_cases.get(fl, 0) + n
        for k, (m, c, n) in o.gens.items():
            g = self.gens.setdefault(k, [set(), set(), 0])
            g[0] |= m
            g[1] |= c
            g[2] += n
        for k, (m, n) in o.pairs.items():
            p = self.pairs.setdefault(k, [set(), 0])
            p[0] |= m
            p[1] += n
        self.cases += o.cases
        self.zero += o.zero
        self.repeats += o.repeats
        self.seeds += o.seeds
        return self


def _pass_analyze(log_path: str, mapper: LineMapper, in_scope, part, guard, removed, repeat_suffix: str = "#again") -> _Acc:
    """`removed`: the lines MC leaves out (the background and the replicate noise), for
    the cases with no MC."""
    acc = _Acc()
    cache: dict = {}

    def window(cov):
        lines = _lines(mapper, {}, cov, cache, in_scope)
        if guard is None:
            return lines
        acc.unguarded |= lines
        return lines & guard

    for _, recs in _seed_records(log_path, part):
        acc.seeds += 1
        for rec in recs:
            if rec["t"] in ("idle", "other"):
                acc.covered |= window(rec["w"])
                continue
            if rec["t"] != "case":
                continue
            w = {v: window(cov) for v, cov in rec["w"].items()}
            for v, lines in w.items():
                acc.covered |= lines
                again = w.get(v + repeat_suffix)
                if again is not None:
                    acc.repeats += 1
                    acc.repeat_diff |= lines ^ again
            members = sorted({v for p in rec["pairs"] for v in p})
            if len(members) < 2:
                continue
            union = frozenset().union(*(w[v] for v in members))
            every = frozenset.intersection(*(w[v] for v in members))
            case_mc = union - every
            acc.cases += 1
            acc.zero += not (case_mc - removed)
            acc.compared |= union
            acc.mc |= case_mc
            for fl in case_mc:
                acc.mc_cases[fl] = acc.mc_cases.get(fl, 0) + 1
            g = acc.gens.setdefault(rec["gen"], [set(), set(), 0])
            g[0] |= case_mc
            g[1] |= union
            g[2] += 1
            for a, b in rec["pairs"]:
                key = "~".join(sorted((data_model_of(a), data_model_of(b))))
                p = acc.pairs.setdefault(key, [set(), 0])
                p[0] |= w[a] ^ w[b]
                p[1] += 1
    return acc


# --- the passes, serial or forked --------------------------------------------------------

_CTX: dict = {}  # what the forked workers share: the line map and the scope


def _run(fn, jobs: list, workers: int) -> list:
    if workers <= 1 or len(jobs) <= 1:
        return [fn(*j) for j in jobs]
    import multiprocessing

    with multiprocessing.get_context("fork").Pool(min(workers, len(jobs))) as pool:
        return pool.starmap(fn, jobs)


def _w_units(log, part):
    return _pass_units(log, _CTX["mapper"], part)


def _w_noise(a, b, part, ga, gb):
    return _pass_noise(a, b, _CTX["mapper"], _CTX["in_scope"], part, ga, gb)


def _w_analyze(log, part, guard, removed):
    return _pass_analyze(log, _CTX["mapper"], _CTX["in_scope"], part, guard, removed)


def cumulative(log_path: str, mapper: LineMapper, in_scope) -> frozenset:
    """The lines the whole run executed: every window of the log merged per unit, then
    mapped (the run's cumulative profile, which the coverage tool's report must match).
    A window line outside it is not one the run executed: with counters a region's count
    is a difference of counts, and a function running across a window's edge (a
    background thread) splits them, so the earlier window seems to run a region it did
    not; the report leaves such lines out and counts them."""
    return _guard_and_background([_pass_units(log_path, mapper, None)], mapper, in_scope)[0]


def replicate_noise(log_a: str, log_b: str, mapper: LineMapper, in_scope, guard_a: "frozenset | None" = None, guard_b: "frozenset | None" = None) -> dict:
    """The lines two independent runs of the same seeds (twins: a fresh server each)
    executed differently for the SAME variant of the SAME case: what a window holds that
    the question does not decide. Seeds are read in lockstep (one worker runs them in
    order); the comparison stops where the shorter run stops. A case that is not the same
    question in both runs (another generator or shape: the runs diverged) is an error."""
    return _merge_noise([_pass_noise(log_a, log_b, mapper, in_scope, None, guard_a, guard_b)])


def analyze(log_path: str, mapper: LineMapper, in_scope, components: Components, noise: "dict | None" = None, repeat_suffix: str = "#again", keep_sets: bool = False, guard: "frozenset | None" = None) -> dict:
    """The report of one run, serially; `noise` is `replicate_noise` of it and its twin
    (taken out of MC with the background); `guard` the run's `cumulative` lines, which
    every window's lines are cut to (None: no guard)."""
    background = _idle_lines(log_path, mapper, in_scope)
    if guard is not None:
        background &= guard
    replicate = noise["lines"] if noise else frozenset()
    acc = _pass_analyze(log_path, mapper, in_scope, None, guard, background | replicate, repeat_suffix)
    return _finish(acc, meta_of(log_path), mapper, in_scope, components, guard, background, noise, keep_sets)


def _idle_lines(log_path: str, mapper: LineMapper, in_scope) -> frozenset:
    out: set = set()
    cache: dict = {}
    for _, recs in _seed_records(log_path):
        for rec in recs:
            if rec["t"] == "idle":
                out |= _lines(mapper, {}, rec["w"], cache, in_scope)
    return frozenset(out)


def evaluate(log_path: str, twin_path: "str | None", mapper: LineMapper, in_scope, components: Components, workers: int = 1) -> "tuple[dict, dict | None]":
    """The run's report and its twin's (None without one), the passes split over
    `workers` forked processes: the guards and backgrounds, then the replicate noise,
    then each run's numbers. Exactly what the serial functions compute."""
    _CTX.update(mapper=mapper, in_scope=in_scope)
    logs = [log_path] + ([twin_path] if twin_path else [])
    n = max(1, workers)
    parts = [(k, n) for k in range(n)]
    units = _run(_w_units, [(log, part) for log in logs for part in parts], workers)
    guards, backgrounds = [], []
    for i, _ in enumerate(logs):
        g, b = _guard_and_background(units[i * n : (i + 1) * n], mapper, in_scope)
        guards.append(g)
        backgrounds.append(b)
    noise = None
    if twin_path:
        noise = _merge_noise(_run(_w_noise, [(log_path, twin_path, part, guards[0], guards[1]) for part in parts], workers))
    replicate = noise["lines"] if noise else frozenset()
    accs = _run(_w_analyze, [(log, part, guards[i], backgrounds[i] | replicate) for i, log in enumerate(logs) for part in parts], workers)
    reports = []
    for i, log in enumerate(logs):
        acc = _Acc()
        for a in accs[i * n : (i + 1) * n]:
            acc.merge(a)
        reports.append(_finish(acc, meta_of(log), mapper, in_scope, components, guards[i], backgrounds[i], noise, keep_sets=bool(twin_path)))
    return reports[0], (reports[1] if twin_path else None)


def _finish(acc: _Acc, meta, mapper: LineMapper, in_scope, components: Components, guard, background: frozenset, noise, keep_sets: bool) -> dict:
    replicate = noise["lines"] if noise else frozenset()
    out_of_mc = background | replicate
    mc_clean = acc.mc - out_of_mc
    executable = {f: ls for f, ls in mapper.executable_lines().items() if in_scope(f)}
    total = sum(len(ls) for ls in executable.values())
    covered = acc.covered

    def pct(n, d):
        return round(100.0 * n / d, 3) if d else 0.0

    comp: dict = {}

    def row(f):
        return comp.setdefault(components.of(f), {"lines": 0, "covered": 0, "mc": 0, "mc_raw": 0, "background": 0, "replicate_noise": 0})

    for f, ls in executable.items():
        row(f)["lines"] += len(ls)
    for f, _ in covered:
        row(f)["covered"] += 1
    for f, _ in mc_clean:
        row(f)["mc"] += 1
    for f, _ in acc.mc:
        row(f)["mc_raw"] += 1
    for f, _ in background:
        row(f)["background"] += 1
    for f, _ in replicate:
        row(f)["replicate_noise"] += 1
    for r in comp.values():
        r["covered_pct"] = pct(r["covered"], r["lines"])
        r["mc_of_covered_pct"] = pct(r["mc"], r["covered"])
        r["share_of_mc_pct"] = pct(r["mc"], len(mc_clean))
    out = {
        "meta": meta,
        "seeds": acc.seeds,
        "cases": acc.cases,
        "executable_lines": total,
        "covered_lines": len(covered),
        "line_coverage_pct": pct(len(covered), total),
        "compared_lines": len(acc.compared),
        "mc_lines_raw": len(acc.mc),
        "background_lines": len(background),
        "replicate_noise_lines": len(replicate),
        "split_window_lines_dropped": len(acc.unguarded - guard) if guard is not None else None,
        "replicate_seeds": noise["seeds"] if noise else 0,
        "replicate_windows": noise["windows"] if noise else 0,
        "mc_lines": len(mc_clean),
        "mc_lines_less_background_only": len(acc.mc - background),
        "mc_of_covered_pct": pct(len(mc_clean), len(covered)),
        "mc_of_executable_pct": pct(len(mc_clean), total),
        "mc_lines_in_one_case_only": sum(1 for fl in mc_clean if acc.mc_cases[fl] == 1),
        "zero_mc_cases": acc.zero,
        "repeats": acc.repeats,
        "repeat_diff_lines": len(acc.repeat_diff),
        "data_model_pairs": {k: {"mc_lines": len(m - out_of_mc), "pairs": n} for k, (m, n) in sorted(acc.pairs.items())},
        "generators": {k: {"mc_lines": len(m - out_of_mc), "compared_lines": len(c), "cases": n} for k, (m, c, n) in sorted(acc.gens.items())},
        "components": dict(sorted(comp.items(), key=lambda kv: -kv[1]["mc"])),
    }
    if keep_sets:
        out["_mc_raw"] = frozenset(acc.mc - background)
        out["_covered"] = frozenset(covered)
    return out


def agreement(a: dict, b: dict) -> dict:
    """How far the twins agree: their MC BEFORE the replicate noise is taken out (each
    less its own background) and their coverage. After it they agree by construction --
    a line one twin has in some case's MC and the other not differs between them in that
    case, so it is noise -- so that comparison would show nothing."""
    def jaccard(x, y):
        return round(len(x & y) / len(x | y), 4) if x | y else 1.0

    ma, mb = a["_mc_raw"], b["_mc_raw"]
    return {"mc_a_before_noise": len(ma), "mc_b_before_noise": len(mb), "mc_both_before_noise": len(ma & mb),
            "mc_jaccard_before_noise": jaccard(ma, mb), "covered_jaccard": jaccard(a["_covered"], b["_covered"])}


def markdown(r: dict, title: str) -> str:
    out = [f"## {title}", "", "| | |", "|---|---|"]
    for label, key in (("seeds", "seeds"), ("cases measured", "cases"), ("executable lines", "executable_lines"), ("covered lines", "covered_lines"),
                       ("line coverage %", "line_coverage_pct"), ("MC lines (less background and replicate noise)", "mc_lines"),
                       ("MC % of covered lines", "mc_of_covered_pct"), ("MC % of executable lines", "mc_of_executable_pct"),
                       ("MC lines raw", "mc_lines_raw"), ("background lines", "background_lines"), ("replicate-noise lines", "replicate_noise_lines"),
                       ("replicate seeds compared", "replicate_seeds"), ("MC lines in one case only", "mc_lines_in_one_case_only"),
                       ("cases with no MC", "zero_mc_cases")):
        out.append(f"| {label} | {r[key]} |")
    out += ["", "| component | executable | covered | covered % | MC | MC % of covered | share of MC % | MC raw | background | replicate noise |", "|---|---|---|---|---|---|---|---|---|---|"]
    for name, c in r["components"].items():
        out.append(f"| {name} | {c['lines']} | {c['covered']} | {c['covered_pct']} | {c['mc']} | {c['mc_of_covered_pct']} | {c['share_of_mc_pct']} | {c['mc_raw']} | {c['background']} | {c['replicate_noise']} |")
    out += ["", "| data-model pair | MC lines | pairs |", "|---|---|---|"]
    for k, v in r["data_model_pairs"].items():
        out.append(f"| {k} | {v['mc_lines']} | {v['pairs']} |")
    return "\n".join(out) + "\n"

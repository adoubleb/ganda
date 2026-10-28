"""Check `ganda.core.engine.llvm_mapping` against llvm-cov on a real build.

    python3 scripts/coverage/check_llvm_mapping.py BINARY PROFILE.profraw \\
        [--window OTHER.profraw ...] [--random 3] [--log WINDOWS.jsonl.gz] [--synthetic 2] \\
        [--llvm-cov llvm-cov] [--llvm-profdata llvm-profdata]

What is compared, file by file and line by line (any difference is printed and fails):

1. cumulative: the decoder's file view (every mapped line and its exact count) against
   `llvm-cov export -format=lcov` (`DA:` lines) of the profile;
2. the decomposition the window log relies on: the union over the profile's units of
   `LlvmLineMapper.unit_lines` (each unit's counters as one window's increments)
   against the lines lcov counts executed, and `executable_lines()` against lcov's
   mapped lines;
3. `llvm-cov report`'s line totals (`export -summary-only`), per file and in all,
   against the decoder's replica of that report;
4. windows: each `--window` profile (another run of the same binary) taken as a window's
   increments, against llvm-cov on that profile alone; `--random N` windows of random
   counter values over random functions (written as a text profile), the same way;
5. `--log`: a campaign's window log against its own cumulative profile (PROFILE): the
   lines its windows executed are llvm-cov's executed lines (less what ran after the
   last window, the shutdown).

`--synthetic N` (with or without PROFILE) checks 1-2 over N random text profiles of the
binary's own records: no run needed, so an object file will do.

PROFILE may be a `.profdata` for 1-3 (the units are then its records). Tools default to
`xcrun llvm-cov` / `xcrun llvm-profdata` on macOS, `llvm-cov` / `llvm-profdata` elsewhere
(for a Rust build: the ones of `rustup component add llvm-tools`, e.g.
`~/.rustup/toolchains/*/lib/rustlib/*/bin/llvm-cov`).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from ganda.core.engine import llvm_mapping as lm  # noqa: E402


def _tool(name: str, given: "str | None") -> list:
    if given:
        return given.split()
    if sys.platform == "darwin" and shutil.which("xcrun"):
        return ["xcrun", name]
    return [name]


class Tools:
    def __init__(self, cov, profdata, tmp):
        self.cov = cov
        self.profdata = profdata
        self.tmp = tmp
        self.n = 0

    def merge(self, inputs: list, text: bool = False) -> str:
        self.n += 1
        out = os.path.join(self.tmp, f"p{self.n}.profdata")
        cmd = self.profdata + ["merge", "-o", out] + (["--text"] if text else []) + inputs
        subprocess.run(cmd, check=True)
        return out

    def lcov(self, binary: str, profdata: str) -> dict:
        r = subprocess.run(self.cov + ["export", "-format=lcov", binary, "-instr-profile", profdata], check=True, capture_output=True, text=True)
        out: dict = {}
        cur = None
        for line in r.stdout.splitlines():
            if line.startswith("SF:"):
                cur = out.setdefault(line[3:], {})
            elif line.startswith("DA:"):
                ln, count = line[3:].split(",")[:2]
                cur[int(ln)] = int(count)
        return out

    def summary(self, binary: str, profdata: str) -> "tuple[dict, dict]":
        r = subprocess.run(self.cov + ["export", "-summary-only", "-format=text", binary, "-instr-profile", profdata], check=True, capture_output=True, text=True)
        data = json.loads(r.stdout)["data"][0]
        files = {f["filename"]: [f["summary"]["lines"]["count"], f["summary"]["lines"]["covered"]] for f in data["files"]}
        t = data["totals"]["lines"]
        return files, {"lines": t["count"], "covered": t["covered"]}


def text_profile(path: str) -> list:
    """[(NameRef, hash, counts)] of a text profile (`llvm-profdata merge --text`)."""
    units = []
    with open(path) as fh:
        lines = [x.rstrip("\n") for x in fh]
    i = 0
    while i < len(lines):
        name = lines[i]
        if not name or name.startswith(":"):
            i += 1
            continue
        if lines[i + 1] != "# Func Hash:":
            raise SystemExit(f"unexpected text profile line {i + 2}: {lines[i + 1]!r}")
        fhash = int(lines[i + 2])
        n = int(lines[i + 4])
        counts = [int(x) for x in lines[i + 6 : i + 6 + n]]
        i += 6 + n
        # value profile data and bitmaps would follow: not in a coverage build
        while i < len(lines) and lines[i]:
            i += 1
        units.append((lm.md5_ref(name.encode()), fhash, counts, name))
    return units


def _diff_files(ours: dict, theirs: dict, what: str, limit: int = 20) -> int:
    ours = {f: v for f, v in ours.items() if v}
    theirs = {f: v for f, v in theirs.items() if v}
    bad = 0
    for f in sorted(set(ours) | set(theirs)):
        a, b = ours.get(f), theirs.get(f)
        if a == b:
            continue
        kind = type(a if a is not None else b)
        a, b = a or kind(), b or kind()
        if kind is dict:
            diff = sorted((ln, a.get(ln), b.get(ln)) for ln in set(a) | set(b) if a.get(ln) != b.get(ln))
        else:
            diff = [("only ours", sorted(set(a) - set(b))[:10]), ("only llvm-cov", sorted(set(b) - set(a))[:10])]
        bad += 1
        if bad <= limit:
            print(f"  MISMATCH {what} {f}: {diff[:12]}")
    return bad


def _executed(lines: dict) -> dict:
    return {f: {ln for ln, c in d.items() if c} for f, d in lines.items() if any(d.values())}


def _union(mapper, windows: dict) -> dict:
    out: dict = {}
    for unit, enc in windows.items():
        for f, ln in mapper.unit_lines(str(unit), None, enc):
            out.setdefault(f, set()).add(ln)
    return out


def _cancelled(b, window: dict, union: dict, theirs: dict) -> set:
    """The lines only the union executes because members of a merged region cancel."""
    extra = {(f, ln) for f, lines in union.items() for ln in lines - theirs.get(f, set())}
    if not extra:
        return set()
    ids = {name: i for i, name in enumerate(b.mapping.filenames)}
    want = {(ids[f], ln): (f, ln) for f, ln in extra}
    out = set()
    for unit, enc in window.items():
        fi = b.unit_function[unit]
        if fi is None:
            continue
        fn, e = b.functions[fi], b.entries[fi]
        counts = dict((c, v) for c, v in enc)
        for k in range(0, len(e), 3):
            key = (e[k], e[k + 1])
            if key in want and lm.evaluate(fn, fn.counted[e[k + 2] * lm.STRIDE + 2], counts, {}) < 0:
                out.add(want[key])
    return out


def _as_window(units: list) -> dict:
    return {u: [[i, c] for i, c in enumerate(counts) if c] for u, (_, _, counts, *_) in enumerate(units) if any(counts)}


def log_lines(mapper, log: str) -> "tuple[dict, int]":
    """{file: lines} every window of a campaign's window log executed (`mc_report`'s
    `covered`), and the number of windows."""
    from ganda.core.engine import window_log

    out: dict = {}
    n = 0
    for rec in window_log.read(log):
        if rec["t"] in ("idle", "other"):
            windows = [rec["w"]]
        elif rec["t"] == "case":
            windows = list(rec["w"].values())
        else:
            continue
        for w in windows:
            n += 1
            for unit, enc in w.items():
                for f, ln in mapper.unit_lines(unit, None, enc):
                    out.setdefault(f, set()).add(ln)
    return out, n


def check(binary: str, profile: str, tools: Tools, windows=(), random_windows: int = 0, seed: int = 1, compilation_dir: str = "", fix=None, log: "str | None" = None) -> int:
    t0 = time.time()
    if profile.endswith(".profdata"):
        text = tools.merge([profile], text=True)
        units = text_profile(text)
        mapper = lm.LlvmLineMapper(binary, [u[:3] for u in units], compilation_dir, fix)
        names = {u[0]: u[3] for u in units}
        profdata = profile
    else:
        mapper = lm.LlvmLineMapper(binary, profile, compilation_dir, fix)
        units, names = lm.profile_units(profile)
        profdata = tools.merge([profile])
    b = mapper.build
    t1 = time.time()
    print(f"decoded {b.stats['records']} records, loaded {b.stats['functions']} functions, {b.stats['regions']} regions, {len(b._per_file)} files in {t1 - t0:.1f} s (covmap Version{b.mapping.version}, line rule of LLVM {'18+' if b.fix else '<18'})")
    print(f"  stats: {b.stats}")
    failures = 0

    # 1. cumulative file view, exact counts
    theirs = tools.lcov(binary, profdata)
    ours = mapper.covered_lines()
    ours = {f: d for f, d in ours.items() if d}
    n_lines = sum(len(d) for d in theirs.values())
    bad = _diff_files(ours, theirs, "count")
    print(f"[1] cumulative: {len(theirs)} files, {n_lines} mapped lines, {sum(len(v) for v in _executed(theirs).values())} executed; files differing: {bad}")
    failures += bad

    # 2. the decomposition
    union = _union(mapper, _as_window(units))
    bad = _diff_files(union, _executed(theirs), "executed (unit union)")
    exe = {f: s for f, s in mapper.executable_lines().items() if s}
    bad2 = _diff_files(exe, {f: set(d) for f, d in theirs.items() if d}, "executable")
    print(f"[2] unit decomposition: executed files differing {bad}, executable files differing {bad2}")
    failures += bad + bad2

    # 3. llvm-cov report's totals
    files, totals = tools.summary(binary, profdata)
    rep = b.report_lines(b.cumulative_values())
    ours_tot = {"lines": sum(v[0] for v in rep.values()), "covered": sum(v[1] for v in rep.values())}
    bad = 0
    for f in sorted(set(files) | set(rep)):
        if files.get(f, [0, 0]) != rep.get(f, [0, 0]):
            bad += 1
            if bad <= 20:
                print(f"  MISMATCH report {f}: ours {rep.get(f)} llvm-cov {files.get(f)}")
    view = {"lines": n_lines, "covered": sum(len(v) for v in _executed(theirs).values())}
    print(f"[3] report: llvm-cov {totals}, ours {ours_tot}, files differing {bad} (file view, lcov DA: {view})")
    failures += bad + (ours_tot != totals)

    # 4. windows
    for w in windows:
        wunits, _ = lm.profile_units(w)
        if [u[:2] for u in wunits] != [u[:2] for u in units]:
            print(f"  {w}: not a profile of the same build (other data records)")
            failures += 1
            continue
        theirs_w = tools.lcov(binary, tools.merge([w]))
        bad = _diff_files(_union(mapper, _as_window(wunits)), _executed(theirs_w), f"window {os.path.basename(w)}")
        print(f"[4] window {w}: {sum(len(v) for v in _executed(theirs_w).values())} executed lines; files differing {bad}")
        failures += bad
    if log:
        # the log's windows, since the server started, add up to the profile: a line
        # executed after the last window (the shutdown) is llvm-cov's only
        covered, n = log_lines(mapper, log)
        theirs_x = _executed(theirs)
        only_ours = sum(len(v - theirs_x.get(f, set())) for f, v in covered.items())
        only_theirs = sum(len(v - covered.get(f, set())) for f, v in theirs_x.items())
        print(f"[5] window log {log}: {n} windows, {sum(len(v) for v in covered.values())} lines covered; llvm-cov {sum(len(v) for v in theirs_x.values())}; only the log's {only_ours} (must be 0), only llvm-cov's {only_theirs} (after the last window)")
        failures += only_ours
    rng = random.Random(seed)
    for k in range(random_windows):
        chosen = set(rng.sample(range(len(units)), max(1, len(units) // 3)))
        wunits, body = [], []
        written = set()
        for u, (ref, fhash, counts, *_) in enumerate(units):
            vals = [rng.choice((0, 0, 0, 1, 2, 3, 7, 1000)) for _ in counts] if u in chosen else [0] * len(counts)
            name = names.get(ref)
            if name is None or (ref, fhash) in written:
                vals = [0] * len(counts)
            else:
                written.add((ref, fhash))
                body.append(f"{name}\n# Func Hash:\n{fhash}\n# Num Counters:\n{len(counts)}\n# Counter Values:\n" + "".join(f"{v}\n" for v in vals) + "\n")
            wunits.append((ref, fhash, vals))
        path = os.path.join(tools.tmp, f"random{k}.proftext")
        with open(path, "w") as fh:
            fh.write("".join(body))
        theirs_w = tools.lcov(binary, tools.merge([path]))
        window = _as_window(wunits)
        union = _union(mapper, window)
        # a merged region (instantiations) whose members' counts cancel: one of them
        # negative, which only inconsistent counters give; the union counts the line,
        # as every instantiation is a function of its own (by design, not a mismatch)
        theirs_x = _executed(theirs_w)
        cancelled = _cancelled(b, window, union, theirs_x)
        for f, ln in cancelled:
            union[f].discard(ln)
        bad = _diff_files(union, theirs_x, f"random window {k}")
        exact = b.line_counts(b.window_values({u: dict(enumerate(c)) for u, (_, _, c) in enumerate(wunits) if any(c)}))
        bad2 = _diff_files({f: d for f, d in exact.items() if d}, theirs_w, f"random window {k} counts")
        print(f"[4] random window {k}: {len(chosen)} functions, {sum(len(v) for v in theirs_x.values())} executed lines; files differing: union {bad}, exact counts {bad2} ({len(cancelled)} lines where a negative count cancels another)")
        failures += bad + bad2
    print(f"{'OK' if not failures else 'FAILED'}: {failures} differences ({time.time() - t0:.1f} s)")
    return failures


def synthetic(binary: str, tools: Tools, n: int, seed: int = 1, compilation_dir: str = "", fix=None) -> int:
    """No profile: `n` random profiles over the binary's own records (an object file
    will do, e.g. an ELF `.o` cross-compiled where no ELF program can run), each checked
    as 1 and 2 above."""
    cm = lm.load_binary(binary, compilation_dir)
    shapes = []
    for name_ref, rec in cm.records.items():
        name = cm.names.get(name_ref)
        if name is None:
            continue
        fn = cm.decode(name_ref)
        needed: set = set()
        for r in fn.regions:
            lm._counters_of(fn, r[1], needed)
            lm._counters_of(fn, r[2], needed)
        shapes.append((name_ref, rec[0], max(needed, default=0) + 1, name))
    rng = random.Random(seed)
    failures = 0
    for k in range(n):
        units, body = [], []
        for name_ref, fhash, size, name in shapes:
            vals = [rng.choice((0, 0, 1, 2, 5, 1000)) for _ in range(size)] if rng.random() < 0.5 else [0] * size
            units.append((name_ref, fhash, vals))
            body.append(f"{name}\n# Func Hash:\n{fhash}\n# Num Counters:\n{size}\n# Counter Values:\n" + "".join(f"{v}\n" for v in vals) + "\n")
        path = os.path.join(tools.tmp, f"synthetic{k}.proftext")
        with open(path, "w") as fh:
            fh.write("".join(body))
        theirs = tools.lcov(binary, tools.merge([path]))
        mapper = lm.LlvmLineMapper(binary, units, compilation_dir, fix)
        b = mapper.build
        bad = _diff_files({f: d for f, d in mapper.covered_lines().items() if d}, theirs, f"synthetic {k} counts")
        window = _as_window([u + (None,) for u in units])
        union = _union(mapper, window)
        theirs_x = _executed(theirs)
        for f, ln in _cancelled(b, window, union, theirs_x):
            union[f].discard(ln)
        bad2 = _diff_files(union, theirs_x, f"synthetic {k} union")
        bad3 = _diff_files({f: s for f, s in mapper.executable_lines().items() if s}, {f: set(d) for f, d in theirs.items() if d}, f"synthetic {k} executable")
        print(f"[s] synthetic profile {k}: {len(shapes)} functions, {sum(len(d) for d in theirs.values())} mapped lines, {sum(len(v) for v in theirs_x.values())} executed; files differing: counts {bad}, union {bad2}, executable {bad3}")
        failures += bad + bad2 + bad3
    print(f"{'OK' if not failures else 'FAILED'}: {failures} differences")
    return failures


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("binary")
    ap.add_argument("profile", nargs="?", help=".profraw (or .profdata); none with --synthetic")
    ap.add_argument("--synthetic", type=int, default=0, help="random profiles over the binary's own records (no run needed)")
    ap.add_argument("--window", action="append", default=[], help="another .profraw of the same binary, taken as a window")
    ap.add_argument("--random", type=int, default=0, help="random windows to check")
    ap.add_argument("--log", help="a campaign's window log (<tag>-windows.jsonl.gz) over PROFILE, the campaign's profile: its windows' lines against llvm-cov's")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--compilation-dir", default="", help="as llvm-cov's -compilation-dir")
    ap.add_argument("--line-rule", choices=("auto", "llvm18", "llvm17"), default="auto", help="the line rule of llvm-cov's version (auto: from the covmap version)")
    ap.add_argument("--llvm-cov")
    ap.add_argument("--llvm-profdata")
    a = ap.parse_args(argv)
    fix = {"auto": None, "llvm18": True, "llvm17": False}[a.line_rule]
    with tempfile.TemporaryDirectory() as tmp:
        tools = Tools(_tool("llvm-cov", a.llvm_cov), _tool("llvm-profdata", a.llvm_profdata), tmp)
        bad = 0
        if a.profile:
            bad += check(a.binary, a.profile, tools, a.window, a.random, a.seed, a.compilation_dir, fix, a.log)
        if a.synthetic:
            bad += synthetic(a.binary, tools, a.synthetic, a.seed, a.compilation_dir, fix)
        if not a.profile and not a.synthetic:
            ap.error("a profile, or --synthetic N")
        return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

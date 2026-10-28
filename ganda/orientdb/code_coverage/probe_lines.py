"""Source lines from JaCoCo probe sets, offline, with JaCoCo's own line semantics.

`collector.py` records a coverage window as {class id: probe mask}. JaCoCo's line
coverage is a union over the probes hit -- a line is covered when an instruction on it
is, and an instruction is covered when a probe after it in its method's control flow is
(the propagation, the filters' merges and branch replacements are all ORs) -- so a map
from every probe to the lines that probe ALONE covers turns any window into its lines:

    lines(P) == union over p in P of lines({p})

`jacoco/ProbeLines.java` builds that map from JaCoCo's own `Analyzer` (every class of the
image's `orientdb-*.jar`s, as `report.py` analyzes them) and checks the union against
direct analyses; `check` compares the map with JaCoCo's line counters and, given
execution data, with JaCoCo's covered lines.

    python3 -m ganda.orientdb.code_coverage.probe_lines build            # coverage/probe-lines-<image id>.jsonl.gz
    python3 -m ganda.orientdb.code_coverage.probe_lines verify --samples 0 --every-probe
    python3 -m ganda.orientdb.code_coverage.probe_lines check            # executable lines and per-package totals
    python3 -m ganda.orientdb.code_coverage.probe_lines check --exec campaign_reports/cov-coverage.exec
    python3 -m ganda.orientdb.code_coverage.probe_lines synth coverage/synthetic.exec --seed 1

A line is `(source path, line)`, the path `<package dir>/<SourceFile>` (an inner class's
lines land on its outer class's file; a class without a SourceFile attribute keys its
lines by its vm name, as `ProbeLines.java lines` does).
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import subprocess
import sys
import tempfile
from dataclasses import dataclass

from ganda.orientdb.code_coverage.collector import AGENT_EXCLUDES, ExecData
from ganda.orientdb.code_coverage.report import classpath, image_lib
from ganda.core.engine.mc_report import LineMapper
from ganda.orientdb.engine.connection import DEFAULT_IMAGE

FORMAT = "ganda-probe-lines/1"
HERE = os.path.dirname(os.path.abspath(__file__))
PROGRAM = os.path.join(HERE, "jacoco", "ProbeLines.java")


@dataclass(frozen=True)
class ClassLines:
    name: str  # vm name (a/b/C$D)
    jar: str
    path: "str | None"  # the source path its lines are keyed by; None: the analyzer skips it (synthetic)
    probes: int
    lines: tuple  # the executable lines
    probe_lines: tuple  # probe i -> the lines it alone covers

    @property
    def package(self) -> str:
        return self.name.rsplit("/", 1)[0] if "/" in self.name else ""


class ProbeLineMap:
    """A map file loaded: `classes` by JaCoCo class id."""

    def __init__(self, header: dict, classes: dict):
        self.header = header
        self.classes = classes

    def lines_of(self, window: dict) -> set:
        """The (source path, line) pairs a window {class id: probe mask} covers. A class
        the map does not hold is not one of the jars' (JaCoCo ignores it too: see
        `unknown`); a probe past a class's count is another build's, an error."""
        out: set = set()
        for cid, mask in window.items():
            c = self.classes.get(cid)
            if c is None or not mask:
                continue
            if mask >> c.probes:
                raise ValueError(f"{c.name}: probe {mask.bit_length() - 1} of a class with {c.probes} probes (another build?)")
            path = c.path
            for i in probes_of(mask):
                out.update((path, line) for line in c.probe_lines[i])
        return out

    def unknown(self, window: dict) -> set:
        """The class ids of a window the map does not hold."""
        return {cid for cid, mask in window.items() if mask and cid not in self.classes}

    def executable(self) -> set:
        """Every (source path, line) with an instruction."""
        return {(c.path, line) for c in self.classes.values() for line in c.lines}

    def by_package(self, lines: set) -> dict:
        """{package (vm form): number of the lines in it}."""
        pkg_of = {c.path: c.package for c in self.classes.values() if c.path}
        out: dict = {}
        for path, _ in lines:
            p = pkg_of[path]
            out[p] = out.get(p, 0) + 1
        return out

    def exec_data(self, window: dict) -> ExecData:
        """The window as execution data (`ExecData.write` makes a `.exec` of it)."""
        data = ExecData()
        for cid, mask in window.items():
            c = self.classes[cid]
            data.add(cid, c.name, c.probes, mask)
        return data


class JacocoLineMapper(LineMapper):
    """The window log's JaCoCo windows ({class id: hex probe mask}) as source lines, for
    `ganda.core.engine.mc_report`."""

    def __init__(self, pmap: ProbeLineMap):
        self.pmap = pmap

    def unit_lines(self, unit: str, name: "str | None", encoded) -> frozenset:
        return frozenset(self.pmap.lines_of({int(unit): int(encoded, 16)}))

    def unit_files(self, unit: str) -> set:
        c = self.pmap.classes.get(int(unit))
        return {c.path} if c is not None and c.path else set()

    def merge(self, encodings: list) -> str:
        """Probes are hit-or-not: the windows' masks OR-ed."""
        mask = 0
        for enc in encodings:
            mask |= int(enc, 16)
        return f"{mask:x}"

    def executable_lines(self) -> dict:
        out: dict = {}
        for path, line in self.pmap.executable():
            out.setdefault(path, set()).add(line)
        return out


def probes_of(mask: int):
    """The set bits of a probe mask, lowest first."""
    bits = bin(mask)[:1:-1]  # bit 0 first
    i = bits.find("1")
    while i >= 0:
        yield i
        i = bits.find("1", i + 1)


def load(path: str) -> ProbeLineMap:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        header = json.loads(fh.readline())
        if header.get("format") != FORMAT:
            raise ValueError(f"{path} is not a {FORMAT} file")
        classes = {}
        for line in fh:
            r = json.loads(line)
            if r["source"] is not None:
                path_ = f"{r['package']}/{r['source']}" if r["package"] else r["source"]
            else:
                path_ = r["name"] if r["analyzed"] else None
            classes[r["id"]] = ClassLines(r["name"], r["jar"], path_, r["probes"], tuple(r["lines"]), tuple(tuple(p) for p in r["probe_lines"]))
    if len(classes) != header["classes"]:
        raise ValueError(f"{path}: {len(classes)} class records, the header says {header['classes']}")
    return ProbeLineMap(header, classes)


# --- the Java side -------------------------------------------------------------------


def default_map(lib: str) -> str:
    """`coverage/probe-lines-<image id>.jsonl.gz` beside the image's `coverage/lib-<image id>`."""
    lib = os.path.abspath(lib)
    return os.path.join(os.path.dirname(lib), "probe-lines-" + os.path.basename(lib).removeprefix("lib-") + ".jsonl.gz")


def run_java(command: str, lib: str, *args: str) -> int:
    cmd = ["java", "-cp", classpath(), PROGRAM, command, "--lib", lib, "--exclude", *AGENT_EXCLUDES.split(":"), *args]
    return subprocess.run(cmd).returncode


def build(lib: str, out: str, threads: "int | None" = None) -> int:
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    return run_java("map", lib, "--out", out, *(["--threads", str(threads)] if threads else []))


def jacoco_lines(lib: str, execs: list) -> dict:
    """JaCoCo itself on `execs` (none: an empty store): {"covered": {path: [lines]},
    "executable": {...}, "packages": {package: [covered, total]}, "no_match": n}."""
    fd, tmp = tempfile.mkstemp(suffix=".json.gz")
    os.close(fd)
    try:
        rc = run_java("lines", lib, "--out", tmp, *(["--exec", *execs] if execs else []))
        if rc != 0:
            raise RuntimeError(f"ProbeLines.java lines exited {rc}")
        with gzip.open(tmp, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    finally:
        os.unlink(tmp)


def _pairs(by_path: dict) -> set:
    return {(p, line) for p, lines in by_path.items() for line in lines}


def check(pmap: ProbeLineMap, lib: str, execs: list) -> int:
    """The map against JaCoCo's own analysis of the same jars: the executable lines and the
    line counters by package; with execution data, the covered lines and counters."""
    jac = jacoco_lines(lib, execs)
    failures = 0

    def same(what: str, mine: set, theirs: set) -> None:
        nonlocal failures
        ok = mine == theirs
        failures += not ok
        print(f"{what}: map {len(mine)}, JaCoCo {len(theirs)}  {'equal' if ok else 'DIFFERENT'}")
        if not ok:
            for label, extra in (("map only", mine - theirs), ("JaCoCo only", theirs - mine)):
                print(f"  {label}: {len(extra)}", sorted(extra)[:10])

    executable = pmap.executable()
    same("executable lines", executable, _pairs(jac["executable"]))
    columns = [("total", 1, pmap.by_package(executable))]
    if execs:
        window = ExecData()
        for f in execs:
            window.merge(ExecData.read(f))
        unknown = pmap.unknown(window.masks)
        names = {c.name for c in pmap.classes.values()}
        if unknown:
            print(f"{len(unknown)} classes of the execution data are not in the map ({sum(window.names[c] in names for c in unknown)} of them by a name it holds: another build); JaCoCo matched {jac['no_match']} no better")
        covered = pmap.lines_of(window.masks)
        same("covered lines", covered, _pairs(jac["covered"]))
        columns.append(("covered", 0, pmap.by_package(covered)))
    totals = jac["packages"]
    for what, col, mine in columns:
        theirs = {p: row[col] for p, row in totals.items() if row[col]}
        ok = mine == theirs
        failures += not ok
        print(f"{what} lines by package (JaCoCo's ICounter, {len(theirs)} packages, {sum(theirs.values())} lines): {'equal' if ok else 'DIFFERENT'}")
        if not ok:
            for p in sorted(set(mine) | set(theirs)):
                if mine.get(p, 0) != theirs.get(p, 0):
                    print(f"  {p}: map {mine.get(p, 0)}, JaCoCo {theirs.get(p, 0)}")
    return 1 if failures else 0


def synthetic_window(pmap: ProbeLineMap, seed: int, class_fraction: float, probe_fraction: float) -> dict:
    """A random window: each class with a probe hit with `class_fraction`, each of its
    probes then with `probe_fraction` (at least one)."""
    rnd = random.Random(seed)
    window = {}
    for cid in sorted(pmap.classes):
        c = pmap.classes[cid]
        if c.probes == 0 or rnd.random() >= class_fraction:
            continue
        mask = sum(1 << i for i in range(c.probes) if rnd.random() < probe_fraction) or 1 << rnd.randrange(c.probes)
        window[cid] = mask
    return window


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["build", "verify", "check", "synth"])
    ap.add_argument("out", nargs="?", help="synth: the .exec to write")
    ap.add_argument("--map", help="the map file (default: coverage/probe-lines-<image id>.jsonl.gz)")
    ap.add_argument("--image", default=DEFAULT_IMAGE)
    ap.add_argument("--lib", help="a directory of orientdb-*.jar instead of the image's")
    ap.add_argument("--threads", type=int)
    ap.add_argument("--exec", nargs="+", default=[], help="check: execution data (.exec); several are merged")
    ap.add_argument("--samples", type=int, default=300, help="verify: classes to check (0: every class with a probe)")
    ap.add_argument("--largest", type=int, default=30, help="verify: the largest classes, always checked")
    ap.add_argument("--draws", type=int, default=3, help="verify: random subsets per size and class")
    ap.add_argument("--every-probe", action="store_true", help="verify: every probe of a checked class alone, not 20 at random")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--class-fraction", type=float, default=0.5, help="synth: the share of classes hit")
    ap.add_argument("--probe-fraction", type=float, default=0.3, help="synth: the share of a hit class's probes")
    args = ap.parse_args(argv)
    lib = args.lib or image_lib(args.image)
    path = args.map or default_map(lib)
    if args.command == "build":
        return build(lib, path, args.threads)
    if args.command == "verify":
        extra = ["--samples", str(args.samples), "--largest", str(args.largest), "--draws", str(args.draws), "--seed", str(args.seed)]
        if args.every_probe:
            extra.append("--every-probe")
        if args.threads:
            extra += ["--threads", str(args.threads)]
        return run_java("verify", lib, "--map", path, *extra)
    pmap = load(path)
    if args.command == "check":
        return check(pmap, lib, args.exec)
    if not args.out:
        ap.error("synth needs the .exec to write")
    window = synthetic_window(pmap, args.seed, args.class_fraction, args.probe_fraction)
    pmap.exec_data(window).write(args.out, session=f"synthetic-{args.seed}")
    print(f"{args.out}: {len(window)} classes, {sum(m.bit_count() for m in window.values())} probes, {len(pmap.lines_of(window))} lines")
    return 0


if __name__ == "__main__":
    sys.exit(main())

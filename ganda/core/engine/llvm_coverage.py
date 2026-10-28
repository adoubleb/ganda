"""Code coverage of a server built with LLVM's source-based coverage, read live.

The server is an instrumented build (clang `-fprofile-instr-generate -fcoverage-mapping`,
rustc `-C instrument-coverage`, both with `-runtime-counter-relocation`) started with
`LLVM_PROFILE_FILE=<dir>/%c%h-%p.profraw`: `%c` is CONTINUOUS mode, in which the counters
live in the `.profraw` file itself (mapped shared), so they can be read while the server
runs, without a dump command and without stopping it. A window is the counters that
changed since the previous read: the file is never written here, and it stays a valid,
cumulative profile for `llvm-profdata merge` / `llvm-cov report` afterwards.

An element is one counter (a region's execution count, as a JaCoCo probe is an edge);
a unit is one instrumented function (its data record), bit i its i-th counter. The
function's name (from the names section, matched by its MD5) gives the module a
breakdown groups by.
"""
from __future__ import annotations

import bisect
import glob
import hashlib
import json
import mmap
import os
import re
import shutil
import struct
import threading
import time
import zlib

from ganda.core.engine import mc as _mc
from ganda.core.engine.coverage import LEVELS, Collector, window_log_path
from ganda.core.engine.window_log import WindowLog
from ganda.core.oracle.case import comparable_pairs
from ganda.core.oracle.oracle import REPEAT

MAGIC = 0xFF6C70726F667281  # "\xfflprofr\x81", 64-bit raw profile
VARIANT_MASK_BYTE_COVERAGE = 1 << 60  # single-byte counters (0 = hit) instead of u64 counts
# header fields per raw version (InstrProfData.inc, INSTR_PROF_RAW_HEADER)
HEADER_FIELDS = {
    # LLVM 14-17 (Apple clang 15-16): no bitmap yet
    8: ("Magic", "Version", "BinaryIdsSize", "NumData", "PaddingBytesBeforeCounters", "NumCounters", "PaddingBytesAfterCounters", "NamesSize", "CountersDelta", "NamesDelta", "ValueKindLast"),
    9: ("Magic", "Version", "BinaryIdsSize", "NumData", "PaddingBytesBeforeCounters", "NumCounters", "PaddingBytesAfterCounters", "NumBitmapBytes", "PaddingBytesAfterBitmapBytes", "NamesSize", "CountersDelta", "BitmapDelta", "NamesDelta", "ValueKindLast"),
    10: ("Magic", "Version", "BinaryIdsSize", "NumData", "PaddingBytesBeforeCounters", "NumCounters", "PaddingBytesAfterCounters", "NumBitmapBytes", "PaddingBytesAfterBitmapBytes", "NamesSize", "CountersDelta", "BitmapDelta", "NamesDelta", "NumVTables", "VNamesSize", "ValueKindLast"),
}
PAGE = 4096
_NONZERO = re.compile(rb"[^\x00]")


class Profile:
    """One continuous-mode `.profraw`, mapped read-only."""

    def __init__(self, path: str):
        self.path = path
        self.fh = open(path, "rb")
        self.mm = mmap.mmap(self.fh.fileno(), 0, access=mmap.ACCESS_READ)
        magic, version = struct.unpack_from("<QQ", self.mm, 0)
        if magic != MAGIC:
            raise ValueError(f"{path}: not a 64-bit raw LLVM profile (magic 0x{magic:x})")
        self.version = version & 0xFFFFFFFF
        if self.version not in HEADER_FIELDS:
            raise ValueError(f"{path}: raw profile version {self.version} not supported ({sorted(HEADER_FIELDS)})")
        names = HEADER_FIELDS[self.version]
        self.header = dict(zip(names, struct.unpack_from(f"<{len(names)}Q", self.mm, 0)))
        h = self.header
        self.num_counters = h["NumCounters"]
        start = len(names) * 8 + h["BinaryIdsSize"]
        # the data records' size is not in the header: what the file's size leaves for
        # them (the sections after the data are all in the header), else the page the
        # counters start on (continuous mode without counter relocation)
        self.data_offset = start
        self.counter_size = 1 if version & VARIANT_MASK_BYTE_COVERAGE else 8
        rest = len(self.mm) - start - h["PaddingBytesBeforeCounters"] - h["NumCounters"] * self.counter_size - h["PaddingBytesAfterCounters"] - h.get("NumBitmapBytes", 0) - h.get("PaddingBytesAfterBitmapBytes", 0) - h["NamesSize"]
        sizes = (48, 56, 64, 72, 80, 88)
        self.data_size = next((d for d in sizes if h["NumData"] and rest - h["NumData"] * d in range(0, 8)), None)
        if self.data_size is None:
            self.data_size = next((d for d in sizes if (start + h["NumData"] * d + h["PaddingBytesBeforeCounters"]) % PAGE == 0), None)
        if self.data_size is None:
            raise ValueError(f"{path}: cannot place the counters (a profile still being written, or a layout this reader does not know)")
        self.counters_offset = start + h["NumData"] * self.data_size + h["PaddingBytesBeforeCounters"]
        self.counters_bytes = self.num_counters * self.counter_size
        self.names_offset = self.counters_offset + self.counters_bytes + h["PaddingBytesAfterCounters"] + h.get("NumBitmapBytes", 0) + h.get("PaddingBytesAfterBitmapBytes", 0)
        self._functions = None
        self._records = None

    def counters(self) -> bytes:
        return self.mm[self.counters_offset : self.counters_offset + self.counters_bytes]

    def close(self) -> None:
        self.mm.close()
        self.fh.close()

    # --- functions: which counters are whose --------------------------------------

    def functions(self) -> "tuple[list, list]":
        """(first counter index of each function, sorted; its name), from the data
        records (each record's CounterPtr is relative to the record itself)."""
        if self._functions is None:
            names = self._names()
            recs = self.records()
            self._functions = ([r[0] for r in recs], [names.get(r[1], f"md5:{r[1]:016x}") for r in recs])
        return self._functions

    def records(self) -> list:
        """(first counter index, NameRef, FuncHash, number of counters) of each data
        record, sorted as `functions()` (a window's unit is a position in it)."""
        if self._records is None:
            h = self.header
            # NumCounters follows NameRef, FuncHash, CounterPtr, [BitmapPtr,] FunctionPointer, Values
            num_at = 40 if self.version == 8 else 48
            out = []
            for i in range(h["NumData"]):
                off = self.data_offset + i * self.data_size
                name_ref, fhash, counter_ptr = struct.unpack_from("<QQq", self.mm, off)
                n, = struct.unpack_from("<I", self.mm, off + num_at)
                # unsigned, as LLVM's reader computes it: the delta may be negative
                rel = (counter_ptr - (h["CountersDelta"] - i * self.data_size)) % (1 << 64)
                out.append((rel // self.counter_size, name_ref, fhash, n))
            out.sort()
            self._records = out
        return self._records

    def _names(self) -> dict:
        """{MD5 low 64 bits: name} of the names section (blocks of ULEB uncompressed
        length, ULEB compressed length, zlib or raw text; names split by \\x01)."""
        raw = self.mm[self.names_offset : self.names_offset + self.header["NamesSize"]]
        out, pos = {}, 0
        while pos < len(raw):
            full, pos = _uleb(raw, pos)
            packed, pos = _uleb(raw, pos)
            if full == 0 and packed == 0:
                break
            if packed:
                text, pos = zlib.decompress(raw[pos : pos + packed]), pos + packed
            else:
                text, pos = raw[pos : pos + full], pos + full
            for name in text.split(b"\x01"):
                out[int.from_bytes(hashlib.md5(name).digest()[:8], "little")] = name.decode("utf-8", "replace")
        return out


def _uleb(buf: bytes, pos: int) -> "tuple[int, int]":
    value = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, pos
        shift += 7


def changed(before: bytes, after: bytes, counter_size: int) -> list:
    """Indices of the counters that differ between two reads, in order."""
    diff = (int.from_bytes(before, "little") ^ int.from_bytes(after, "little")).to_bytes(len(after), "little")
    out = []
    last = -1
    for m in _NONZERO.finditer(diff):
        i = m.start() // counter_size
        if i != last:
            out.append(i)
            last = i
    return out


def module_of(name: str, depth: int = 2) -> str:
    """The leading components of a mangled function name: `_ZN<len><id>...` (a C++
    namespace path, a legacy Rust path) or Rust's v0 `_R...` (its crate root
    `C[s<hash>_]<len><crate>` and the identifier after it), else the name's prefix."""
    name = re.sub(r"^[^;:]*\.(?:c|cc|cpp|cxx|h|hh|hpp|rs)[;:]", "", name)  # a local function is `<file>;<name>` (`:` in older LLVMs)
    if name.startswith("_R"):
        m = re.search(r"C(?:s[0-9A-Za-z]*_)?(\d+)", name)
        if m:
            parts, pos = [], m.start(1)
            while len(parts) < depth:
                n = re.match(r"(\d+)_?", name[pos:])
                if not n:
                    break
                k = int(n.group(1))
                start = pos + len(n.group())
                parts.append(name[start : start + k])
                pos = start + k
            if parts:
                return "::".join(parts)
    m = re.match(r"_ZN(?:K|V|r)*", name)
    if m:
        pos, parts = m.end(), []
        while len(parts) < depth:
            n = re.match(r"\d+", name[pos:])
            if not n:
                break
            k = int(n.group())
            start = pos + len(n.group())
            parts.append(name[start : start + k])
            pos = start + k
        if parts:
            return "::".join(parts)
    return name[:24]


class LlvmCollector(Collector):
    """Windows over a continuous-mode profile: `seed`, `case` or `variant` level, as the
    campaign asks; at `variant` level also the metamorphic coverage of every case
    (`ganda.core.engine.mc`). `profile_dir` is where the server writes its `.profraw`
    (one per process, `<container>-<pid>.profraw`: the live server is PID 1 of the
    newest container)."""

    def __init__(self, profile_dir: str, level: str, wait: float = 60.0, scope=None, idle_seconds: float = 5.0, idle_every: float = 900.0, log_path: "str | None" = None, meta: "dict | None" = None):
        """`scope(name) -> bool` keeps the functions the summary measures (a build that
        instruments the libraries it bundles too); every function if None. The window
        log keeps every function: what is in scope is decided afterwards, by file."""
        if level not in LEVELS[1:]:
            raise ValueError(f"coverage level {level!r} not one of {LEVELS[1:]}")
        self.profile_dir = profile_dir
        self.scope = scope
        self.in_scope: "set | None" = None
        self.level = level
        self.wait = wait
        self.lock = threading.Lock()
        self.profile: "Profile | None" = None
        self.last: "bytes | None" = None
        self.total: dict = {}
        self.generators: dict = {}
        self.growth: list = []
        self.windows = 0
        self.start = time.time()
        self.mc = _mc.MetamorphicCoverage() if level == "variant" else None
        self.case_windows: dict = {}
        self.calibrated = idle_seconds <= 0
        self.idle_seconds = idle_seconds
        self.idle_every = idle_every
        self.last_idle = time.time()
        self.log = WindowLog(log_path, {"element": "llvm_counter", "profile_dir": profile_dir, "idle_seconds": idle_seconds, "idle_every": idle_every, **(meta or {})}) if log_path and level == "variant" else None
        self.case_counts: dict = {}
        if level in ("case", "variant"):
            self.window("boot")

    # --- reading ---------------------------------------------------------------------

    def _open(self) -> Profile:
        deadline = time.time() + self.wait
        while True:
            # the container's PID 1 is the server (an entrypoint that initializes the
            # database first runs it as a child, which leaves its own file)
            files = sorted(glob.glob(os.path.join(self.profile_dir, "*.profraw")), key=lambda f: (f.endswith("-1.profraw"), os.path.getmtime(f)))
            if files and (self.profile is None or files[-1] != self.profile.path):
                try:
                    return Profile(files[-1])
                except (ValueError, struct.error):
                    pass  # still being written
            if self.profile is not None and files and files[-1] == self.profile.path:
                return self.profile
            if time.time() > deadline:
                raise RuntimeError(f"no continuous-mode .profraw in {self.profile_dir}: is the server an instrumented build started with LLVM_PROFILE_FILE=.../%c%h-%p.profraw?")
            time.sleep(0.5)

    def delta(self) -> dict:
        """The counters that moved since the previous read, as {function: mask}, of
        the functions in scope."""
        return self.read()[0]

    def read(self) -> "tuple[dict, dict]":
        """({function in scope: mask}, {function: [[counter, increment], ...]}) of the
        counters that moved since the previous read."""
        profile = self._open()
        if profile is not self.profile:
            if self.profile is not None:
                self.profile.close()
            self.profile, self.last = profile, bytes(profile.counters_bytes) if profile.counter_size == 8 else b"\xff" * profile.counters_bytes
            if self.scope is not None:
                names = profile.functions()[1]
                self.in_scope = {f for f, name in enumerate(names) if self.scope(name)}
        now = self.profile.counters()
        size = self.profile.counter_size
        hit = changed(self.last, now, size)
        before, self.last = self.last, now
        starts, _ = self.profile.functions()
        masks: dict = {}
        counts: dict = {}
        for i in hit:
            f = bisect.bisect_right(starts, i) - 1
            inc = struct.unpack_from("<Q", now, 8 * i)[0] - struct.unpack_from("<Q", before, 8 * i)[0] if size == 8 else 1
            counts.setdefault(f, []).append([i - starts[f], inc])
            if self.in_scope is None or f in self.in_scope:
                masks[f] = masks.get(f, 0) | (1 << (i - starts[f]))
        return masks, counts

    def counters_in_scope(self) -> int:
        if self.profile is None:
            return 0
        if self.in_scope is None:
            return self.profile.num_counters
        starts = self.profile.functions()[0]
        ends = starts[1:] + [self.profile.num_counters]
        return sum(ends[f] - starts[f] for f in self.in_scope)

    def window(self, label: "str | None" = None, variant: "str | None" = None) -> None:
        with self.lock:
            d, counts = self.read()
            self.windows += 1
            _mc._or(self.total, d)
            if label:
                _mc._or(self.generators.setdefault(label, {}), d)
            if variant and self.mc is not None:
                self.case_windows[variant] = d
                if self.log is not None:
                    self.case_counts[variant] = counts
                    names = self.profile.functions()[1]
                    self.log.name_units({str(f): names[f] for f in counts})
            elif self.log is not None:
                names = self.profile.functions()[1]
                self.log.name_units({str(f): names[f] for f in counts})
                self.log.other(label, {str(f): pairs for f, pairs in counts.items()})

    # --- the campaign's hooks -----------------------------------------------------

    def after_setup(self) -> None:
        if self.level in ("case", "variant"):
            self.window("setup")
        if self.mc is not None and not self.calibrated:
            self.calibrated = True
            self._idle()

    def _idle(self) -> None:
        """Idle windows: their scoped masks to the background, every counter to the log."""
        with self.lock:
            moved: dict = {}
            reads: list = []

            def dump():
                masks, counts = self.read()
                if reads:
                    for f, pairs in counts.items():
                        moved.setdefault(f, []).extend(pairs)
                elif self.log is not None:  # idle_windows' first read closes the window before
                    self.log.other("setup", {str(f): pairs for f, pairs in counts.items()})
                reads.append(1)
                return masks

            self.mc.add_background(_mc.idle_windows(dump, self.idle_seconds))
            self.last_idle = time.time()
            if self.log is not None:
                names = self.profile.functions()[1]
                self.log.name_units({str(f): names[f] for f in moved})
                self.log.idle({str(f): pairs for f, pairs in moved.items()})

    def after_case(self, case) -> None:
        if self.level == "case":
            self.window(case.generator_name)
        if self.mc is not None:
            self.mc.add_case(case, self.case_windows)
            if self.log is not None:
                firsts = [v for v in self.case_counts if not v.endswith(REPEAT)]
                self.log.case(case, comparable_pairs(case, firsts), {v: {str(f): pairs for f, pairs in w.items()} for v, w in self.case_counts.items()})
            self.case_windows = {}
            self.case_counts = {}

    def after_variant(self, case, variant: str) -> None:
        if self.level == "variant":
            self.window(case.generator_name, variant)

    def before_restart(self) -> None:
        self.window()

    def after_seed(self, seed: int) -> None:
        self.window("setup" if self.level in ("case", "variant") else None)
        self.growth.append({"instances": len(self.growth) + 1, "seed": seed, "elapsed": round(time.time() - self.start, 1), "counters": _mc._count(self.total)})
        if self.mc is not None and time.time() - self.last_idle >= self.idle_every:
            self._idle()
        if self.log is not None:
            self.log.seed_done(seed)

    # --- the results ---------------------------------------------------------------

    def _module(self, unit: int) -> str:
        return module_of(self.profile.functions()[1][unit]) if self.profile else "?"

    def summary(self) -> dict:
        total = self.counters_in_scope()
        by: dict = {}
        for unit, mask in self.total.items():
            by[self._module(unit)] = by.get(self._module(unit), 0) + mask.bit_count()
        out = {
            "level": self.level,
            "profile": self.profile.path if self.profile else None,
            "scope": "every function" if self.scope is None else f"{len(self.in_scope or ())} functions in scope",
            "counters_covered": _mc._count(self.total),
            "counters": total,
            "functions_hit": len(self.total),
            "windows": self.windows,
            "growth": self.growth,
            "modules": dict(sorted(by.items(), key=lambda kv: -kv[1])[:40]),
        }
        if self.generators:
            out["generators"] = {name: _mc._count(cov) for name, cov in sorted(self.generators.items())}
        if self.mc is not None:
            out["mc"] = self.mc.summary(unit_name=self._module, denominator=total)
        return out

    def write(self, stats_dir: str, tag: str) -> None:
        """The cumulative profiles (`<tag>-coverage/*.profraw`) for llvm-profdata / llvm-cov."""
        d = os.path.join(stats_dir, f"{tag}-coverage")
        os.makedirs(d, exist_ok=True)
        for f in glob.glob(os.path.join(self.profile_dir, "*.profraw")):
            shutil.copy(f, d)
        if self.log is not None:
            self.log.close()

    def record(self, summary: dict, stats_dir: str, tag: str) -> dict:
        path = os.path.join(stats_dir, f"{tag}-coverage.json")
        with open(path, "w") as fh:
            json.dump(summary, fh, indent=2)
        print(f"coverage written to {path} (profiles: {tag}-coverage/*.profraw)")
        out = {k: summary[k] for k in ("level", "counters_covered", "counters", "functions_hit", "windows")}
        out["json"] = path
        if "mc" in summary:
            out["mc"] = {k: summary["mc"][k] for k in _mc.HEADLINE}
        return out

    def print_summary(self, s: dict) -> None:
        pct = 100.0 * s["counters_covered"] / s["counters"] if s["counters"] else 0.0
        print(f"coverage ({s['level']}): {s['counters_covered']} of {s['counters']} counters ({pct:.1f}%) in {s['functions_hit']} functions, {s['windows']} windows")
        for name, n in list(s["modules"].items())[:12]:
            print(f"  {name:52s} {n:7d}")
        if "mc" in s:
            _mc.print_summary(s["mc"])


def profile_dir(args) -> "str | None":
    """Where a campaign's server writes its profile under `--coverage`:
    `<stats dir>/<tag>-profile`, emptied and open to the container's user."""
    if getattr(args, "coverage", "off") == "off":
        return None
    d = os.path.abspath(os.path.join(args.stats_dir, f"{args.run_tag}-profile"))
    os.makedirs(d, exist_ok=True)
    os.chmod(d, 0o777)
    for f in glob.glob(os.path.join(d, "*.profraw")):
        os.remove(f)
    return d


def collector(server, args, scope=None, meta: "dict | None" = None) -> "LlvmCollector | None":
    """The provider's `coverage` slot for a server started with `profile_dir(args)`;
    `scope` as `LlvmCollector`'s, `meta` what the window log records of the build."""
    level = getattr(args, "coverage", "off")
    if level == "off":
        return None
    if not getattr(server, "profile_dir", None):
        raise SystemExit("--coverage reads the profile of a server this campaign starts (an instrumented image), not an external one")
    return LlvmCollector(server.profile_dir, level, scope=scope, log_path=window_log_path(args), meta={"image": getattr(server, "image", None), **(meta or {})})


CONTAINER_DIR = "/ganda-profile"
# `%c`: continuous mode; `%h`: the container's hostname (its id), so a restarted
# server never reuses a file; `%p`: the process
PROFILE_FILE = f"{CONTAINER_DIR}/%c%h-%p.profraw"

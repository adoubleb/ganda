"""Code coverage of the OrientDB server under test, through the JaCoCo agent.

With `OrientInstance(coverage=True)` the server JVM runs with JaCoCo's agent
(`output=tcpserver`, `includes=com.orientechnologies.*`), which counts PROBES -- one per
edge of every instrumented method's control flow, so a probe set is branch coverage
without the class files. The fuzzer speaks the agent's TCP protocol itself: a DUMP
command with `reset` answers the probes hit since the previous dump (the agent writes
only the classes with a hit), so every execution window -- one seed, one case, one side
-- is its own delta, and `Collector` ORs each delta into the cumulative map and into
the window's labels. A probe set is a Python int per class (bit i = probe i: the wire
packs the booleans least-significant bit first, so the bytes ARE the little-endian int).

What this module sees is the loaded classes only. Line / branch / method coverage
against every class in the jars (the never-loaded ones included) is
`scripts/coverage_report.py`'s job, from the `.exec` files `Collector.write` leaves --
the format `jacococli` and every JaCoCo tool read.
"""
from __future__ import annotations

import io
import json
import os
import re
import socket
import struct
import threading
import time
from dataclasses import dataclass, field

JACOCO_VERSION = "0.8.13"
AGENT_PORT = 6300
AGENT_INCLUDES = "com.orientechnologies.*"
# the JavaCC lexer: `jjMoveNfa_0` outgrows the 64 KB method limit once probed
# (MethodTooLargeException in the server log), so the agent would skip it anyway
AGENT_EXCLUDES = "com.orientechnologies.orient.core.sql.parser.OrientSqlTokenManager"
AGENT_IN_CONTAINER = "/jacoco/jacocoagent.jar"

BLOCK_HEADER = 0x01
BLOCK_SESSIONINFO = 0x10
BLOCK_EXECUTIONDATA = 0x11
BLOCK_CMD_OK = 0x20
BLOCK_CMD_DUMP = 0x40
MAGIC = 0xC0C0
FORMAT_VERSION = 0x1007  # every JaCoCo since 0.7.5
HEADER = struct.pack(">BHH", BLOCK_HEADER, MAGIC, FORMAT_VERSION)

LEVELS = ("off", "seed", "case", "side")


def agent_jar() -> str:
    """The agent jar mounted into the container: `JACOCO_AGENT`, or the runtime jar
    Maven keeps (`org.jacoco.agent-<v>-runtime.jar` is the agent itself)."""
    path = os.environ.get("JACOCO_AGENT") or os.path.expanduser(
        f"~/.m2/repository/org/jacoco/org.jacoco.agent/{JACOCO_VERSION}/org.jacoco.agent-{JACOCO_VERSION}-runtime.jar"
    )
    if not os.path.isfile(path):
        raise RuntimeError(f"no JaCoCo agent at {path}: set JACOCO_AGENT to a jacocoagent.jar")
    return path


def agent_java_opts(port: int = AGENT_PORT) -> str:
    # address=* : the default loopback is unreachable through Docker's port forward
    return f"-javaagent:{AGENT_IN_CONTAINER}=output=tcpserver,address=*,port={port},includes={AGENT_INCLUDES},excludes={AGENT_EXCLUDES}"


# --- the execution-data format -----------------------------------------------------


@dataclass
class ExecData:
    """Probe sets by JaCoCo class id (a CRC64 of the class bytes)."""

    names: dict = field(default_factory=dict)  # class id -> vm name (a/b/C)
    sizes: dict = field(default_factory=dict)  # class id -> probe count
    masks: dict = field(default_factory=dict)  # class id -> int, bit i = probe i hit

    def add(self, cid: int, name: str, size: int, mask: int) -> None:
        self.names[cid] = name
        self.sizes[cid] = size
        self.masks[cid] = self.masks.get(cid, 0) | mask

    def merge(self, other: "ExecData") -> int:
        """OR `other` in; the number of probes that were new here."""
        new = 0
        for cid, m in other.masks.items():
            old = self.masks.get(cid, 0)
            new += (m & ~old).bit_count()
            self.add(cid, other.names[cid], other.sizes[cid], m)
        return new

    def minus(self, other: "ExecData") -> "ExecData":
        out = ExecData()
        for cid, m in self.masks.items():
            rest = m & ~other.masks.get(cid, 0)
            if rest:
                out.add(cid, self.names[cid], self.sizes[cid], rest)
        return out

    def covered(self) -> int:
        return sum(m.bit_count() for m in self.masks.values())

    def loaded_probes(self) -> int:
        """Every probe of the classes with a hit -- the denominator this module can see."""
        return sum(self.sizes[cid] for cid, m in self.masks.items() if m)

    def classes_hit(self) -> int:
        return sum(1 for m in self.masks.values() if m)

    def by_package(self, depth: int = 5) -> dict:
        """{package prefix: [covered, probes of the hit classes, classes hit]}, the package
        cut at `depth` components (5: `com.orientechnologies.orient.core.sql`)."""
        out: dict = {}
        for cid, m in self.masks.items():
            if not m:
                continue
            pkg = ".".join(self.names[cid].split("/")[:-1][:depth])
            row = out.setdefault(pkg, [0, 0, 0])
            row[0] += m.bit_count()
            row[1] += self.sizes[cid]
            row[2] += 1
        return out

    def to_bytes(self, session: str = "ganda", start_ms: int = 0, dump_ms: int = 0) -> bytes:
        buf = io.BytesIO()
        buf.write(HEADER)
        buf.write(bytes([BLOCK_SESSIONINFO]))
        _write_utf(buf, session)
        buf.write(struct.pack(">qq", start_ms, dump_ms))
        for cid in sorted(self.masks):
            size = self.sizes[cid]
            buf.write(bytes([BLOCK_EXECUTIONDATA]))
            buf.write(struct.pack(">q", cid))
            _write_utf(buf, self.names[cid])
            _write_varint(buf, size)
            buf.write(self.masks[cid].to_bytes((size + 7) // 8, "little"))
        return buf.getvalue()

    def write(self, path: str, session: str = "ganda") -> None:
        now = int(time.time() * 1000)
        with open(path, "wb") as fh:
            fh.write(self.to_bytes(session, now, now))

    @classmethod
    def read(cls, path: str) -> "ExecData":
        with open(path, "rb") as fh:
            data = cls()
            _read_blocks(fh, data, until_ok=False)
            return data

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ExecData":
        data = cls()
        _read_blocks(io.BytesIO(raw), data, until_ok=False)
        return data


def _write_utf(buf, s: str) -> None:
    b = s.encode("utf-8")
    buf.write(struct.pack(">H", len(b)))
    buf.write(b)


def _write_varint(buf, v: int) -> None:
    while v & ~0x7F:
        buf.write(bytes([0x80 | (v & 0x7F)]))
        v >>= 7
    buf.write(bytes([v]))


def _exact(fh, n: int) -> bytes:
    b = fh.read(n)
    if len(b) != n:
        raise EOFError(f"execution data truncated: wanted {n} bytes, got {len(b)}")
    return b


def _varint(fh) -> int:
    value, shift = 0, 0
    while True:
        b = _exact(fh, 1)[0]
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value
        shift += 7


def _utf(fh) -> str:
    (n,) = struct.unpack(">H", _exact(fh, 2))
    return _exact(fh, n).decode("utf-8", "replace")


def _read_blocks(fh, data: ExecData, until_ok: bool) -> None:
    """Blocks until EOF (a file) or until the agent's CMD_OK (a dump answer)."""
    first = True
    while True:
        t = fh.read(1)
        if not t:
            if until_ok:
                raise EOFError("the agent closed the connection before CMD_OK")
            return
        kind = t[0]
        if first and kind != BLOCK_HEADER:
            raise ValueError(f"not JaCoCo execution data (first block 0x{kind:02x})")
        first = False
        if kind == BLOCK_HEADER:
            magic, version = struct.unpack(">HH", _exact(fh, 4))
            if magic != MAGIC or version != FORMAT_VERSION:
                raise ValueError(f"execution data magic/version 0x{magic:04x}/0x{version:04x}, expected 0x{MAGIC:04x}/0x{FORMAT_VERSION:04x}")
        elif kind == BLOCK_SESSIONINFO:
            _utf(fh)
            _exact(fh, 16)
        elif kind == BLOCK_EXECUTIONDATA:
            (cid,) = struct.unpack(">q", _exact(fh, 8))
            name = _utf(fh)
            size = _varint(fh)
            mask = int.from_bytes(_exact(fh, (size + 7) // 8), "little")
            data.add(cid, name, size, mask)
        elif kind == BLOCK_CMD_OK:
            if until_ok:
                return
        else:
            raise ValueError(f"unknown execution-data block 0x{kind:02x}")


class _AckingReader:
    """Reads the agent's answer and answers every chunk with a header block.

    The agent writes its answer unbuffered, a few bytes per `write`, from a socket with
    Nagle on: the first small segment goes out and the rest waits for its ACK, which
    the receiver delays by the kernel's minimum (40 ms on Linux -- inside the container
    too, measured) because it has nothing to send. That stall was nearly all of a dump
    (11,710 dumps, 530 s of a 667 s campaign). A header block is a no-op to the agent's
    reader (`ExecutionDataReader.readHeader`), and the data segment carrying it carries
    the ACK, so the rest of the answer follows at once."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = bytearray()

    def read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                break
            self.buf += chunk
            self.sock.sendall(HEADER)
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out


def dump(host: str, port: int, reset: bool = True, timeout: float = 60.0) -> ExecData:
    """One DUMP command against a running agent. The agent serves one connection at a
    time, so callers serialize (`Collector` holds a lock)."""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.sendall(HEADER + bytes([BLOCK_CMD_DUMP, 1, 1 if reset else 0]))
        data = ExecData()
        _read_blocks(_AckingReader(sock), data, until_ok=True)
        return data


# --- the campaign's collector ------------------------------------------------------


def unique_counts(labels: dict) -> dict:
    """{label: probes that label hit and no other label did}."""
    once: dict = {}
    twice: dict = {}
    for data in labels.values():
        for cid, m in data.masks.items():
            o = once.get(cid, 0)
            twice[cid] = twice.get(cid, 0) | (o & m)
            once[cid] = o | m
    return {
        name: sum((m & ~twice.get(cid, 0)).bit_count() for cid, m in data.masks.items())
        for name, data in labels.items()
    }


class Collector:
    """Takes a reset dump after every execution window and attributes it.

    `seed`: one window per seed -- the growth curve only, any number of workers.
    `case`: a window after every case, labelled by its template; `side`: after every
    side, labelled by the template and by `template/side`. Both attribute a window to
    whatever ran since the last dump, so they need `--workers 1`. Two labels are not
    templates: `boot` (the server's start) and `setup` (the database lifecycle --
    create, DDL, inserts, state checks, drop). A dump that fails (the server crashed)
    loses the window since the last one; the count is reported."""

    def __init__(self, server, level: str):
        if level not in LEVELS[1:]:
            raise ValueError(f"coverage level {level!r} not one of {LEVELS[1:]}")
        self.server = server
        self.level = level
        self.lock = threading.Lock()
        self.total = ExecData()
        self.templates: dict = {}
        self.sides: dict = {}
        self.growth: list = []
        # the `llm_card` saturation curve: each card case's window, in run order, with
        # the probes no earlier card case had reached (case level only)
        self.card_union = ExecData()
        self.card_novelty: list = []
        self.dumps = 0
        self.dump_failures = 0
        self.start = time.time()
        if level in ("case", "side"):
            self.window("boot")

    def window(self, template: "str | None" = None, side: "str | None" = None, card: "str | None" = None) -> None:
        with self.lock:
            try:
                host, port = self.server.coverage_address()
                delta = dump(host, port, reset=True)
            except (OSError, EOFError, ValueError, RuntimeError):
                self.dump_failures += 1
                return
            self.dumps += 1
            self.total.merge(delta)
            if template:
                self.templates.setdefault(template, ExecData()).merge(delta)
            if side:
                self.sides.setdefault(f"{template}/{side}", ExecData()).merge(delta)
            if card:
                self.card_novelty.append({"card": card, "new": self.card_union.merge(delta), "covered": self.card_union.covered()})

    def after_setup(self) -> None:
        if self.level in ("case", "side"):
            self.window("setup")

    def after_case(self, case) -> None:
        if self.level == "case":
            self.window(case.template_name, card=case.params.get("card") if case.template_name == "llm_card" else None)

    def after_side(self, case, side: str) -> None:
        if self.level == "side":
            self.window(case.template_name, side)

    def before_restart(self) -> None:
        """The server is about to be replaced: whatever it still holds, or the loss."""
        self.window()

    def after_seed(self, seed: int) -> None:
        if self.level in ("case", "side"):
            self.window("setup")
        else:
            self.window()
        with self.lock:
            self.growth.append({
                "instances": len(self.growth) + 1,
                "seed": seed,
                "elapsed": round(time.time() - self.start, 1),
                "probes": self.total.covered(),
                "classes": self.total.classes_hit(),
            })

    # --- the results ---------------------------------------------------------------

    def summary(self) -> dict:
        t = self.total
        pkgs = t.by_package()
        out = {
            "level": self.level,
            "agent_includes": AGENT_INCLUDES,
            "probes_covered": t.covered(),
            "probes_in_hit_classes": t.loaded_probes(),
            "classes_hit": t.classes_hit(),
            "dumps": self.dumps,
            "dump_failures": self.dump_failures,
            "growth": self.growth,
            "packages": {p: {"covered": c, "probes": n, "classes": k} for p, (c, n, k) in sorted(pkgs.items(), key=lambda kv: -kv[1][0])},
        }
        if self.templates:
            uniq = unique_counts(self.templates)
            out["templates"] = {
                name: {"covered": d.covered(), "unique": uniq[name], "classes": d.classes_hit()}
                for name, d in sorted(self.templates.items(), key=lambda kv: -uniq[kv[0]])
            }
        if self.card_novelty:
            per_card: dict = {}
            for w in self.card_novelty:
                per_card[w["card"]] = per_card.get(w["card"], 0) + w["new"]
            out["card_novelty"] = {"curve": self.card_novelty, "per_card": dict(sorted(per_card.items(), key=lambda kv: -kv[1]))}
        if self.sides:
            uniq = unique_counts(self.sides)
            ranked = sorted(self.sides.items(), key=lambda kv: (-uniq[kv[0]], -kv[1].covered()))
            out["sides"] = {name: {"covered": d.covered(), "unique": uniq[name]} for name, d in ranked}
        return out

    def write_exec(self, stats_dir: str, tag: str) -> None:
        """`<tag>-coverage.exec` (cumulative) and, when attributed,
        `<tag>-coverage/<template>.exec` for `coverage_report.py diff`."""
        os.makedirs(stats_dir, exist_ok=True)
        self.total.write(os.path.join(stats_dir, f"{tag}-coverage.exec"), session=tag)
        if self.templates:
            d = os.path.join(stats_dir, f"{tag}-coverage")
            os.makedirs(d, exist_ok=True)
            for name, data in self.templates.items():
                data.write(os.path.join(d, f"{_safe(name)}.exec"), session=f"{tag}/{name}")


def write_summary(summary: dict, stats_dir: str, tag: str) -> str:
    path = os.path.join(stats_dir, f"{tag}-coverage.json")
    with open(path, "w") as fh:
        json.dump(summary, fh, indent=2)
    return path


HEADLINE = ("level", "probes_covered", "probes_in_hit_classes", "classes_hit", "dumps", "dump_failures")


def print_summary(s: dict, top: int = 12) -> None:
    pct = 100.0 * s["probes_covered"] / s["probes_in_hit_classes"] if s["probes_in_hit_classes"] else 0.0
    print(f"coverage ({s['level']}): {s['probes_covered']} probes in {s['classes_hit']} classes "
          f"({pct:.1f}% of the probes of the classes hit)  dumps: {s['dumps']}  lost windows: {s['dump_failures']}")
    for pkg, row in list(s["packages"].items())[:top]:
        print(f"  {pkg:52s} {row['covered']:7d} / {row['probes']:7d}  classes={row['classes']}")
    if "templates" in s:
        print("  by template (covered / unique to it):")
        for name, row in s["templates"].items():
            print(f"    {name:26s} {row['covered']:7d} / {row['unique']:6d}")
    if "sides" in s:
        print("  top sides by unique probes (covered / unique to it):")
        for name, row in list(s["sides"].items())[:top]:
            print(f"    {name:44s} {row['covered']:7d} / {row['unique']:6d}")


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)

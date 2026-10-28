"""The raw coverage of a campaign, window by window: the evidence every coverage number
is computed from afterwards (`ganda/core/engine/mc_report.py`).

`<stats dir>/<tag>-windows.jsonl.gz`, one JSON record per line, appended as the campaign
runs (each seed's records are flushed when the seed ends, so a stopped campaign keeps
every finished seed):

- `{"t": "meta", ...}`: the engine, the element kind, the collector's settings;
- `{"t": "unit", "u": unit, "name": ...}`: a unit's name, the first time it is seen
  (a JaCoCo class id and its class; an LLVM function index and its mangled name);
- `{"t": "case", "gen": ..., "shape": ..., "pairs": [[a, b], ...], "w": {variant:
  coverage}}`: one case, its compared pairs and the coverage of every variant asked (a
  variant asked again for the A/A check is `<variant>#again`);
- `{"t": "idle", "w": coverage}`: what the server reached while nothing was asked;
- `{"t": "other", "label": ..., "w": coverage}`: every other window (`boot`, `setup`:
  the database lifecycle, the writes), so that the log holds every element the
  campaign reached;
- `{"t": "seed", "seed": n, "elapsed": s}`: a seed is done (the records since the
  previous one are its).

A coverage is {unit: element set}, in the collector's encoding: for JaCoCo a hex bitmask
of the class's probes; for LLVM `[[counter, increment], ...]` within the function (the
increments, not just "it moved", because a region's count is an expression over them).
"""
from __future__ import annotations

import gzip
import json
import threading
import time


class WindowLog:
    def __init__(self, path: str, meta: dict):
        self.path = path
        self.fh = gzip.open(path, "at", compresslevel=6)
        self.lock = threading.Lock()
        self.named: set = set()
        self.start = time.time()
        self.records = 0
        self._write({"t": "meta", "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **meta})

    def _write(self, rec: dict) -> None:
        self.fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        self.records += 1

    def name_units(self, names: dict) -> None:
        """{unit: name} for units not named yet."""
        with self.lock:
            for unit, name in names.items():
                if unit not in self.named:
                    self.named.add(unit)
                    self._write({"t": "unit", "u": unit, "name": name})

    def case(self, case, pairs: list, windows: dict) -> None:
        with self.lock:
            self._write({"t": "case", "gen": case.generator_name, "shape": getattr(case, "shape", None), "pairs": [list(p) for p in pairs], "w": windows})

    def idle(self, coverage: dict) -> None:
        with self.lock:
            self._write({"t": "idle", "w": coverage})

    def other(self, label: "str | None", coverage: dict) -> None:
        if coverage:
            with self.lock:
                self._write({"t": "other", "label": label, "w": coverage})

    def seed_done(self, seed) -> None:
        with self.lock:
            self._write({"t": "seed", "seed": seed, "elapsed": round(time.time() - self.start, 1)})
            self.fh.flush()

    def close(self) -> None:
        with self.lock:
            self.fh.close()


def read(path: str):
    """Every record of a window log, in order (a log cut short by a crash ends at its
    last complete record)."""
    with gzip.open(path, "rt") as fh:
        try:
            for line in fh:
                if line.endswith("\n"):
                    yield json.loads(line)
        except (EOFError, gzip.BadGzipFile):
            return

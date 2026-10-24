"""The engine build a provider's probe last verified (`<provider>/rules/baseline.json`):
the build, the date, the checks that held and the defects fixed there (each with the
checks that no longer reproduce it). A failing check of a rule changes the oracle's
assumptions, so it blocks the baseline; a failing check of a defect only means its guard
withholds what the build answers right, so it is recorded. A run of some checks merges
into a baseline of the same build. A campaign started against another build warns
(`warning`)."""
from __future__ import annotations

import dataclasses
import datetime
import json
import pathlib
import re


def _natural(fid: str) -> list:
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", fid)]


@dataclasses.dataclass(frozen=True)
class Baseline:
    path: pathlib.Path

    def read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}

    def record(self, version: str, held: "list[str]", fixed: "dict[str, list[str]]", blocking: "list[str]", partial: bool = False) -> str:
        """Write the baseline after a run on `version`: the checks that `held`, the
        defects `fixed` there ({defect id: the checks that no longer reproduce it}), and
        the failing checks of rules (or of nothing) that are `blocking`; `partial`: the
        run covered some checks, merged into a baseline of the same build. Returns what
        was done."""
        if blocking:
            return f"baseline not written: {len(blocking)} check(s) of rules failed on {version}"
        ran = set(held) | {f for fids in fixed.values() for f in fids}
        old_held: set = set()
        old_fixed: dict = {}
        if partial:
            base = self.read()
            if base.get("version") != version:
                return f"baseline not written: a run of some checks on {version} cannot update a baseline of {base.get('version') or 'no build'} (run every check)"
            old_held = set(base["held"]) - ran
            old_fixed = {d: [f for f in fids if f not in ran] for d, fids in base["fixed"].items()}
        merged = {d: sorted({*old_fixed.get(d, ()), *fixed.get(d, ())}, key=_natural) for d in {*old_fixed, *fixed}}
        merged = {d: fids for d, fids in sorted(merged.items(), key=lambda kv: _natural(kv[0])) if fids}
        base = {"version": version, "probed": datetime.date.today().isoformat(), "held": sorted(old_held | set(held), key=_natural), "fixed": merged}
        self.path.write_text(json.dumps(base, indent=1) + "\n")
        return f"baseline written: {self.path} ({len(base['held'])} checks held on {version}" + (f"; defects fixed there: {', '.join(merged)})" if merged else ")")

    def warning(self, engine: str, version: "str | None") -> "str | None":
        """The warning for a campaign against a server of `version`, or None (the
        baseline's build, no baseline yet, or a server that cannot say its build)."""
        base = self.read()
        if version is None or not base or base["version"] == version:
            return None
        return (f"{engine}: the server is {version}, the baseline {base['version']} ({base['probed']}): the guards may withhold "
                f"defects this build fixed. Run `ganda {engine} probe`.")

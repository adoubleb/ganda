"""The evaluation's numbers from one campaign's window log: line coverage, line-based
metamorphic coverage and its component breakdown (`ganda.core.engine.mc_report`).

    python3 scripts/coverage/mc_report.py orientdb --log R/<tag>-windows.jsonl.gz \\
        --probe-map coverage/probe-lines-<image id>.jsonl.gz --out R/<tag>-mc
    python3 scripts/coverage/mc_report.py surrealdb --log R/<tag>-windows.jsonl.gz \\
        --binary surreal --profile R/<tag>-coverage/<container>-1.profraw --out R/<tag>-mc

The scope and the components are `scripts/coverage/components/<engine>.json`, written
before the campaigns ran. `--out` gets `report.json` (every number, the inputs' SHA-256)
and `report.md` (the tables).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from ganda.core.engine import mc_report  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("engine", choices=["orientdb", "surrealdb", "arangodb"])
    ap.add_argument("--log", required=True, help="the campaign's <tag>-windows.jsonl.gz")
    ap.add_argument("--twin", help="the window log of its twin (the same seeds on a fresh server): the replicate noise, and the agreement of the two runs")
    ap.add_argument("--components", help="default: scripts/coverage/components/<engine>.json")
    ap.add_argument("--probe-map", help="orientdb: the probe-to-lines map (ganda.orientdb.code_coverage.probe_lines build)")
    ap.add_argument("--binary", help="surrealdb / arangodb: the instrumented binary the campaign ran")
    ap.add_argument("--profile", help="surrealdb / arangodb: the campaign's cumulative .profraw (its function records)")
    ap.add_argument("--out", required=True, help="directory for report.json and report.md")
    ap.add_argument("--workers", type=int, default=16, help="forked processes the passes are split over, by seed (default: %(default)s)")
    args = ap.parse_args(argv)
    spec_path = args.components or os.path.join(HERE, "components", f"{args.engine}.json")
    with open(spec_path) as fh:
        spec = json.load(fh)
    inputs = [args.log, spec_path] + ([args.twin] if args.twin else [])
    if args.engine == "orientdb":
        if not args.probe_map:
            ap.error("orientdb needs --probe-map")
        from ganda.orientdb.code_coverage.probe_lines import JacocoLineMapper, load

        mapper = JacocoLineMapper(load(args.probe_map))
        inputs.append(args.probe_map)
    else:
        if not (args.binary and args.profile):
            ap.error(f"{args.engine} needs --binary and --profile")
        from ganda.core.engine.llvm_mapping import LlvmLineMapper

        mapper = LlvmLineMapper(args.binary, args.profile)
        inputs += [args.binary, args.profile]
    scope = re.compile(spec["scope"])
    started = time.time()
    in_scope = lambda f: bool(scope.search(f))  # noqa: E731
    components = mc_report.Components(spec["components"])
    report, twin = mc_report.evaluate(args.log, args.twin, mapper, in_scope, components, workers=args.workers)
    if twin is not None:
        report["twin"] = {k: twin[k] for k in ("seeds", "cases", "covered_lines", "line_coverage_pct", "mc_lines", "mc_of_covered_pct")}
        report["agreement"] = mc_report.agreement(report, twin)
    report.pop("_mc_raw", None), report.pop("_covered", None)
    report["inputs"] = {os.path.basename(p): sha256(p) for p in inputs}
    report["scope"] = spec["scope"]
    report["analysis_seconds"] = round(time.time() - started, 1)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "report.json"), "w") as fh:
        json.dump(report, fh, indent=2, default=list)
    text = mc_report.markdown(report, f"{args.engine}: {os.path.basename(args.log)}")
    if args.twin:
        text += "\n| twin agreement | |\n|---|---|\n" + "".join(f"| {k} | {v} |\n" for k, v in report["agreement"].items())
        text += "".join(f"| twin {k} | {v} |\n" for k, v in report["twin"].items())
    with open(os.path.join(args.out, "report.md"), "w") as fh:
        fh.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

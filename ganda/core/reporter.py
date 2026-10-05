"""Writes a self-contained, replayable bug bundle to `bugs/` (gitignored).

    bugs/<tag>-<seed>-<template>-<shape slug>/
        README.md          what diverged, in one screen
        queries.json       every data model's text, the bind variables, rows, mismatches
        instance.json      the abstract instance
        <data model>_setup.json  the setup plan per data model (engine-id placeholders as {"$ref": id})
        repro.py           regenerates the case from (seed, config) and replays it
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
from typing import Any

from ganda.core.setup_plan import plan_to_dict
from ganda.core.model import FieldType


def _jsonable(v: Any) -> Any:
    if isinstance(v, FieldType):
        return v.value
    if dataclasses.is_dataclass(v):
        return {k: _jsonable(x) for k, x in dataclasses.asdict(v).items()}
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, float) and v != v:
        return "NaN"
    return v


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")[:60] or "case"


def bundle_dir(out_dir: str, tag: str, seed: int, template: str, shape: str) -> str:
    return os.path.join(out_dir, f"{tag}-{seed}-{template}-{_slug(shape)}")


def instance_to_dict(instance) -> dict:
    return {
        "schema": _jsonable(instance.schema),
        "nodes": [_jsonable(n) for n in instance.nodes],
        "edges": [_jsonable(e) for e in instance.edges],
        "churn": [{"op": type(op).__name__, **_jsonable(op)} for op in instance.churn],
        "write_forms": {s: _jsonable(form) for s, form in instance.write_forms.items()},
    }


def report_bug(engine, out_dir: str, tag: str, seed: int, cfg, instance, plans: dict, result, times_seen: int = 1) -> str:
    case = result.case
    d = bundle_dir(out_dir, tag, seed, case.generator_name, case.shape)
    os.makedirs(d, exist_ok=True)
    queries = {
        "engine": engine.name,
        "generator": case.generator_name,
        "shape": case.shape,
        "comparison": case.comparison.value,
        "finding_class": result.finding_class,
        "failing_pairs": result.failing_pairs,
        "queries": case.queries,
        "bind_vars": case.bind_vars,
        "params": case.params,
        "known_bugs": case.known_bugs,
        "suppressed": result.suppressed,
        "rows": result.rows,
        "errors": result.errors,
        "internal": result.internal,
        "plans": result.plans,
        "mismatches": [_jsonable(m) for m in result.mismatches],
        "reference_mismatches": [_jsonable(m) for m in result.reference_mismatches],
        "duplicate_id_rows": [_jsonable(x) for x in result.duplicate_id_rows],
    }
    with open(os.path.join(d, "queries.json"), "w") as fh:
        json.dump(queries, fh, indent=2, ensure_ascii=False, default=repr)
    with open(os.path.join(d, "instance.json"), "w") as fh:
        json.dump(instance_to_dict(instance), fh, indent=2, ensure_ascii=False, default=repr)
    for variant, plan in plans.items():
        with open(os.path.join(d, f"{variant}_setup.json"), "w") as fh:
            json.dump(plan_to_dict(plan), fh, indent=2, ensure_ascii=False, default=repr)
    with open(os.path.join(d, "repro.py"), "w") as fh:
        fh.write(_repro_script(engine, seed, cfg, case))
    with open(os.path.join(d, "README.md"), "w") as fh:
        fh.write(_readme(seed, cfg, result, times_seen))
    return d


def report_setup_problem(engine, out_dir: str, tag: str, seed: int, cfg, instance, plans: dict, errors: dict, diffs: dict, kind: str, write_step: "int | None" = None) -> str:
    d = os.path.join(out_dir, f"{tag}-{seed}-{kind}")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "setup.json"), "w") as fh:
        json.dump({"kind": kind, "errors": errors, "state_diffs": {s: x.problems for s, x in diffs.items()}}, fh, indent=2, ensure_ascii=False, default=repr)
    with open(os.path.join(d, "instance.json"), "w") as fh:
        json.dump(instance_to_dict(instance), fh, indent=2, ensure_ascii=False, default=repr)
    for variant, plan in plans.items():
        with open(os.path.join(d, f"{variant}_setup.json"), "w") as fh:
            json.dump(plan_to_dict(plan), fh, indent=2, ensure_ascii=False, default=repr)
    with open(os.path.join(d, "repro.py"), "w") as fh:
        fh.write(_repro_script(engine, seed, cfg, None, write_step))
    with open(os.path.join(d, "README.md"), "w") as fh:
        lines = [f"# {kind} (seed {seed})", ""]
        for s, e in errors.items():
            lines.append(f"- **{s}**: {e or 'ok'}")
        for s, x in diffs.items():
            for p in x.problems:
                lines.append(f"- **{s}** state: {p}")
        fh.write("\n".join(lines) + "\n")
    return d


def report_crash(engine, out_dir: str, tag: str, seed: int, cfg, message: str, diagnostics: str) -> str:
    d = os.path.join(out_dir, f"{tag}-{seed}-crash")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "crash.txt"), "w") as fh:
        fh.write(message + "\n\n" + diagnostics)
    with open(os.path.join(d, "repro.py"), "w") as fh:
        fh.write(_repro_script(engine, seed, cfg, None))
    return d


def _readme(seed, cfg, result, times_seen) -> str:
    case = result.case
    lines = [f"# {case.generator_name} -- {result.finding_class} (seed {seed})", ""]
    lines.append(f"shape: `{case.shape}`  comparison: `{case.comparison.value}`  seen {times_seen}x under this key")
    lines.append("")
    if result.failing_pairs:
        lines.append(f"failing pairs: {', '.join(result.failing_pairs)}")
    if result.suppressed:
        lines.append(f"withheld variants (known bugs): {result.suppressed}")
    lines.append("")
    lines.append("## Queries")
    for variant in case.variants:
        lines.append(f"- **{variant}**: `{case.queries[variant]}`")
    lines.append(f"- bind: `{json.dumps(case.bind_vars, ensure_ascii=False, default=repr)}`")
    lines.append("")
    if any(result.errors.values()):
        lines.append("## Errors")
        for s, e in result.errors.items():
            if e:
                lines.append(f"- **{s}**{' (internal)' if result.internal.get(s) else ''}: {e[:600]}")
        lines.append("")
    if result.mismatches:
        lines.append("## Mismatches (first 12)")
        for m in result.mismatches[:12]:
            lines.append(f"- `{m.record_id}` {m.pair}: {json.dumps(m.left_normalized, ensure_ascii=False, default=repr)[:200]} vs {json.dumps(m.right_normalized, ensure_ascii=False, default=repr)[:200]}")
        lines.append("")
    if result.reference_mismatches:
        lines.append("## Reference mismatches (first 12)")
        for m in result.reference_mismatches[:12]:
            lines.append(f"- `{m.record_id}` {m.variant}: got {json.dumps(m.got, ensure_ascii=False, default=repr)[:200]} expected {json.dumps(m.expected, ensure_ascii=False, default=repr)[:200]}")
        lines.append("")
    if result.duplicate_id_rows:
        lines.append("## Duplicate id rows")
        for x in result.duplicate_id_rows:
            lines.append(f"- {x.variant}: `{x.record_id}` x{x.count}")
        lines.append("")
    lines.append(f"config: `{json.dumps(dataclasses.asdict(cfg))}`")
    return "\n".join(lines) + "\n"


def _repro_script(engine, seed: int, cfg, case, write_step: "int | None" = None) -> str:
    cfg_json = json.dumps(dataclasses.asdict(cfg))
    key_variant = next((x for x in engine.data_models if x in case.queries), None) if case else None
    key = {"generator": case.generator_name, "shape": case.shape, "variant": key_variant, "text": case.queries[key_variant]} if case else None
    # a write-phase case (or a read-back after one) replays the phase up to its step
    step = write_step
    keep_known_bugs = False
    if case is not None and (case.generator_name == "ir_write" or "after_write" in case.params):
        step = case.params.get("step", case.params.get("after_write"))
        keep_known_bugs = case.params.get("keep_known_bugs", False)
    if step is not None:
        key = {**(key or {}), "write_step": step, "keep_known_bugs": keep_known_bugs}
    key = json.dumps(key)
    return f'''#!/usr/bin/env python3
"""Replay: regenerate the instance from (seed, config), materialize it on a fresh
server of the engine and re-run the case (`ganda.core.replay`). Run it from the ganda
project root, or with ganda installed."""
import json
import os
import sys

sys.path.insert(0, os.getcwd())  # the project root, when ganda is not installed
from ganda.core.replay import replay  # noqa: E402

sys.exit(replay({engine.name!r}, {seed}, json.loads({cfg_json!r}), json.loads({key!r})))
'''

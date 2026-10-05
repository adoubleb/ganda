#!/usr/bin/env python3
"""Seed-for-seed parity of the OrientDB provider with the legacy OrientDB fuzzer.

For every (profile, seed) the offline output of the pipeline is canonicalized and
hashed section by section: the schema and instance, the three setup plans, every read
case (texts, bind variables, params, known bugs), every case's reference answer and
the write phase's cases over the evolving instance. No server is involved.

    python3 scripts/golden_dump.py --legacy ../OrientDB --write     # record the baseline
    python3 scripts/golden_dump.py --check                          # ganda against it
    python3 scripts/golden_dump.py --dump default 3 > new.json      # one seed in full
    python3 scripts/golden_dump.py --legacy ../OrientDB --dump default 3 > old.json

Ganda renamed the legacy vocabulary; `RENAMED_KEYS` / `RENAMED_VARIANT_TAGS` translate
the legacy output before hashing, so the comparison stays byte for byte.
"""
from __future__ import annotations

import argparse
import dataclasses
import decimal
import enum
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.path.join(HERE, "..", "tests", "golden", "orientdb.json")

SEEDS = range(1, 41)
# every probability axis of the c100d campaign command, on
AXES = dict(custom_key_probability=0.3, inheritance_probability=0.5, narrow_type_probability=0.3, date_map_probability=0.5, collate_probability=0.4, clusters_probability=0.3, list_index_probability=0.5, churn_edge_update_probability=0.2)
PROFILES = {
    "default": dict(select_cases=40),
    "axes": dict(AXES, select_cases=40),
    # the sizes every report up to c100e ran with
    "small": dict(AXES, max_node_types=4, max_edge_types=5, min_nodes_per_type=3, max_nodes_per_type=14, min_edges_per_type=4, max_edges_per_type=20, ir_cases=10, select_cases=180),
}

# legacy dict key / dataclass field -> ganda name
RENAMED_KEYS: dict = {}
# legacy variant tag (the part after `<data model>:`) -> ganda tag
RENAMED_VARIANT_TAGS: dict = {}


def _rename_string(s: str) -> str:
    m = re.fullmatch(r"(doc|graph|rel):(.+)", s)
    if m and m.group(2) in RENAMED_VARIANT_TAGS:
        return f"{m.group(1)}:{RENAMED_VARIANT_TAGS[m.group(2)]}"
    return s


def canon(value, legacy: bool = False):
    """A JSON value that depends on the content only: no class names, no dict order."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return canon({f.name: getattr(value, f.name) for f in dataclasses.fields(value)}, legacy)
    if isinstance(value, enum.Enum):
        return value.name
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key = k if isinstance(k, str) else json.dumps(canon(k, legacy), sort_keys=True)
            if legacy:
                key = _rename_string(RENAMED_KEYS.get(key, key))
            out[key] = canon(v, legacy)
        return out
    if isinstance(value, (list, tuple)):
        return [canon(v, legacy) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((canon(v, legacy) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        return {"f": repr(value)}
    if isinstance(value, decimal.Decimal):
        return {"d": str(value)}
    if isinstance(value, bytes):
        return {"b": value.hex()}
    if isinstance(value, str):
        return _rename_string(value) if legacy else value
    raise TypeError(f"golden_dump cannot canonicalize {type(value).__name__}: {value!r}")


def _case(case, legacy: bool):
    return canon(case, legacy)


def _legacy_api(root: str):
    sys.path.insert(0, os.path.abspath(root))
    from fuzzer import writes
    from fuzzer.generator import GeneratorConfig, build
    from fuzzer.materialize import BUILDERS
    from fuzzer.materialize.common import plan_to_dict
    from fuzzer.queries.templates import all_cases, reference_for
    from fuzzer.queries.write_generator import sample_write
    from fuzzer.queries.write_ir import apply

    def cases(rng, schema, instance, cfg):
        return all_cases(rng, schema, instance, ir_cases=cfg.ir_cases, select_cases=cfg.select_cases, path_cases=cfg.path_cases)

    def write_steps(rng, schema, instance, cfg):
        return _write_steps(rng.for_template("ir_write"), schema, instance, cfg.write_cases, sample_write, apply, writes.build_write_case, writes._readbacks)

    return dict(config=GeneratorConfig, build=build, mappers=BUILDERS, plan_to_dict=plan_to_dict, cases=cases, reference_for=reference_for, write_steps=write_steps)


def _ganda_api():
    sys.path.insert(0, os.path.join(HERE, ".."))
    from ganda.orientdb import writes
    from ganda.orientdb.generator import GeneratorConfig, build
    from ganda.orientdb.materialize import BUILDERS
    from ganda.orientdb.materialize.common import plan_to_dict
    from ganda.orientdb.queries.templates import all_cases, reference_for
    from ganda.orientdb.queries.write_generator import sample_write
    from ganda.orientdb.queries.write_ir import apply

    def cases(rng, schema, instance, cfg):
        return all_cases(rng, schema, instance, ir_cases=cfg.ir_cases, select_cases=cfg.select_cases, path_cases=cfg.path_cases)

    def write_steps(rng, schema, instance, cfg):
        return _write_steps(rng.for_template("ir_write"), schema, instance, cfg.write_cases, sample_write, apply, writes.build_write_case, writes._readbacks)

    return dict(config=GeneratorConfig, build=build, mappers=BUILDERS, plan_to_dict=plan_to_dict, cases=cases, reference_for=reference_for, write_steps=write_steps)


def _write_steps(rng, schema, instance, steps, sample_write, apply, build_write_case, readbacks):
    """The write phase without a server: (case, the instance its reference reads)."""
    out = []
    inst = instance
    for step in range(steps):
        drawn = sample_write(rng, schema, inst, step, False)
        if drawn is None:
            continue
        q, rewrites, syntaxes = drawn
        after = apply(q, inst)
        case, statements = build_write_case(q, rewrites, syntaxes, step, inst, after, False)
        out.append((case, inst, {dm: (st.prelude, st.main, st.door) for dm, st in statements.items()}))
        inst = after
        for c in readbacks(q, rng, inst, step):
            out.append((c, inst, None))
    return out, inst


def snapshot(api: dict, profile: str, seed: int, legacy: bool) -> dict:
    cfg = api["config"](**PROFILES[profile])
    schema, instance, rng = api["build"](seed, cfg)
    cases = api["cases"](rng, schema, instance, cfg)
    writes, final = api["write_steps"](rng, schema, instance, cfg)
    return {
        "model": canon(instance, legacy),
        "plans": {dm: canon(api["plan_to_dict"](build(instance)), legacy) for dm, build in api["mappers"].items()},
        "cases": [_case(c, legacy) for c in cases],
        "references": [canon(api["reference_for"](c, instance), legacy) for c in cases],
        "writes": [{"case": _case(c, legacy), "reference": canon(api["reference_for"](c, inst), legacy), "statements": canon(st, legacy)} for c, inst, st in writes],
        "final": canon(final, legacy),
    }


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()[:20]


def digests(api: dict, profile: str, seed: int, legacy: bool) -> dict:
    return {section: digest(v) for section, v in snapshot(api, profile, seed, legacy).items()}


def all_digests(api: dict, legacy: bool, seeds=SEEDS) -> dict:
    return {profile: {str(seed): digests(api, profile, seed, legacy) for seed in seeds} for profile in PROFILES}


def load_golden() -> dict:
    with open(GOLDEN) as f:
        return json.load(f)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--legacy", metavar="DIR", help="read the legacy OrientDB fuzzer at DIR instead of ganda")
    ap.add_argument("--write", action="store_true", help="record the digests as the golden baseline")
    ap.add_argument("--check", action="store_true", help="compare against the golden baseline")
    ap.add_argument("--dump", nargs=2, metavar=("PROFILE", "SEED"), help="print one seed's canonical output in full")
    args = ap.parse_args(argv)
    legacy = bool(args.legacy)
    api = _legacy_api(args.legacy) if legacy else _ganda_api()
    if args.dump:
        json.dump(snapshot(api, args.dump[0], int(args.dump[1]), legacy), sys.stdout, indent=1, sort_keys=True)
        return 0
    got = all_digests(api, legacy)
    if args.write:
        with open(GOLDEN, "w") as f:
            json.dump(got, f, indent=1, sort_keys=True)
        print(f"wrote {GOLDEN}")
        return 0
    want = load_golden()
    bad = [(p, s, sec) for p in want for s in want[p] for sec in want[p][s] if got[p][s].get(sec) != want[p][s][sec]]
    for p, s, sec in bad[:40]:
        print(f"MISMATCH profile={p} seed={s} section={sec}")
    print(f"{sum(len(v) for v in want.values())} (profile, seed) pairs, {len(bad)} section mismatches")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

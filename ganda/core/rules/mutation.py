"""The catalog mutation check: does the provider's code depend on each record?

One mutant per alternative answer of a rule (`alternatives`) and one per defect
removed. A mutant runs in a fresh interpreter (`python -m ganda.core.rules.mutation`)
where `Catalog` is built with the change, then every module the read index scans is
imported -- an `assumes` / `works_around` that no longer matches fails there -- and the
setup plans, read cases and write steps of a few seeds are generated with no engine
(with the engine limits the core sizes large data by). A record read only at run time
-- by the connection, say -- is beyond it.
The mutant is CAUGHT by the import or by a change in that output, and SURVIVES
otherwise: the record is read by nothing that decides anything over those seeds."""
from __future__ import annotations

import concurrent.futures
import dataclasses
import enum
import hashlib
import importlib
import json
import math
import os
import pathlib
import subprocess
import sys
import traceback

from ganda.core.rules.model import Catalog, Rule

ROOT = pathlib.Path(__file__).resolve().parents[3]
# the generator axes the second configuration turns on (those the provider's
# configuration has): the narrow and map types, collation, lists, inheritance, keys
AXES = dict(inheritance_probability=0.5, narrow_type_probability=0.3, date_map_probability=0.5, collate_probability=0.4,
            list_index_probability=0.5, custom_key_probability=0.3, churn_edge_update_probability=0.2)


def alternatives(rule: Rule) -> list:
    """Answers the rule could have had instead: every other member of an enum, the other
    truth value, the next count, a bound far below (its square root: a halved bound
    still clears every sum the value pools make), a different operator list or
    character."""
    a, domain = rule.answer, rule.question.domain
    if isinstance(domain, type) and issubclass(domain, enum.Enum):
        return [m for m in domain if m != a]
    if domain is bool:
        return [not a]
    if domain is int:
        return [a + 1 if abs(a) <= 2 else math.isqrt(abs(a))]
    if domain is float:
        return [a / 2 if a else 1.0]
    tokens = a.split()
    return [" ".join(tokens[:-1])] if len(tokens) > 1 else ["@" if a != "@" else "#"]


@dataclasses.dataclass(frozen=True)
class Mutant:
    """A rule given its `alternatives(rule)[k]`, or a defect removed."""

    question: str = ""
    subject: str = ""
    k: int = 0
    defect: str = ""

    def apply(self, rules: tuple, defects: tuple) -> "tuple[tuple, tuple]":
        if self.defect:
            return rules, tuple(d for d in defects if d.id != self.defect)
        return tuple(dataclasses.replace(r, answer=alternatives(r)[self.k]) if (r.question.name, r.subject.value) == (self.question, self.subject) else r for r in rules), defects


def mutants(catalog: Catalog) -> "list[tuple[Mutant, str]]":
    """(mutant, its label) for every alternative of every rule and every defect."""
    from ganda.core.rules.render import value_text

    out = []
    for r in catalog.rules:
        for k, alt in enumerate(alternatives(r)):
            out.append((Mutant(r.question.name, r.subject.value, k), f"{r.question.name} ({r.subject.value}): {value_text(r.answer)} -> {value_text(alt)}"))
    out += [(Mutant(defect=d.id), f"without {d.id} ({d.subject.value})") for d in catalog.defects]
    return out


# --- one mutant, in its own interpreter ----------------------------------------------------


def _modules(engine: str) -> list:
    """The modules the provider's read index scans (`rules.SOURCES` less `rules.EXCLUDE`)."""
    rules = importlib.import_module(f"ganda.{engine}.rules")
    names = []
    for src in rules.SOURCES:
        path = ROOT / src
        files = [path] if path.is_file() else sorted(path.rglob("*.py"))
        for f in files:
            rel = f.relative_to(ROOT)
            if any(part in rules.EXCLUDE for part in rel.parts):
                continue
            names.append(".".join(rel.with_suffix("").parts).removesuffix(".__init__"))
    return names


def _generated(engine_name: str, seeds: int) -> str:
    """A digest of what a campaign would send over `seeds` seeds in two configurations:
    the setup plans, the read cases and the write steps (applied to the Instance), and
    the engine limits the core sizes large data by (`Provider.limits`)."""
    from ganda.core.data.generator import build
    from ganda.core.query.generators.registry import all_cases
    from ganda.core.query.generators.write import sample_write
    from ganda.core.query.generators.write_phase import build_write_case
    from ganda.core.query.ir.write import apply

    from ganda.registry import provider

    engine = provider(engine_name)
    config = engine.config_class
    names = {f.name for f in dataclasses.fields(config)}
    digest = hashlib.sha256(repr(engine.limits).encode())

    def case(c) -> dict:
        return {"q": c.queries, "b": c.bind_vars, "k": c.known_bugs, "o": c.variant_options, "p": bool(c.params.get("partial"))}

    for cfg in (config(), config(**{k: v for k, v in AXES.items() if k in names})):
        for seed in range(1, seeds + 1):
            schema, instance, rng = build(seed, cfg, engine)
            rec: dict = {"plans": {dm: [repr(s) for s in engine.mappers[dm](instance)] for dm in engine.data_models}}
            rec["reads"] = [case(c) for c in all_cases(rng, schema, instance, cfg=cfg)]
            writes, inst, wrng = [], instance, rng.for_generator("ir_write")
            for step in range(cfg.write_cases):
                drawn = sample_write(wrng, inst.schema, inst, step, False)
                if drawn is None:
                    continue
                q, rewrites, syntaxes = drawn
                after = apply(q, inst)
                c, statements = build_write_case(q, rewrites, syntaxes, step, inst, after, False)
                writes.append([case(c), {k: repr(v) for k, v in statements.items()}])
                inst = after
            rec["writes"] = writes
            digest.update(json.dumps(rec, sort_keys=True, default=repr).encode())
    return digest.hexdigest()


def _where(exc: BaseException) -> str:
    """The provider line an import failed at."""
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if "/ganda/core/" not in f.filename and "importlib" not in f.filename]
    f = frames[-1] if frames else traceback.extract_tb(exc.__traceback__)[-1]
    return f"{pathlib.Path(f.filename).resolve().relative_to(ROOT)}:{f.lineno}"


def _run_one(engine: str, mutant: "Mutant | None", seeds: int) -> dict:
    """In this interpreter, before any provider module: build the catalog with the
    mutant, import the scanned modules, digest the generated output."""
    if mutant is not None:
        init = Catalog.__init__

        def mutated(self, name, subjects, rules, defects=(), errors=(), checks=None):
            if name == engine:
                rules, defects = mutant.apply(tuple(rules), tuple(defects))
            init(self, name, subjects, rules, defects, errors, checks if checks is not None else {})

        Catalog.__init__ = mutated
    try:
        for name in _modules(engine):
            importlib.import_module(name)
    except Exception as e:  # noqa: BLE001 -- any failure to import is the catch
        return {"import": f"{type(e).__name__} at {_where(e)}: {str(e).splitlines()[0] if str(e) else ''}"}
    return {"digest": _generated(engine, seeds)}


def _spawn(engine: str, mutant: "Mutant | None", seeds: int) -> dict:
    spec = json.dumps(dataclasses.asdict(mutant) if mutant else None)
    proc = subprocess.run([sys.executable, "-m", "ganda.core.rules.mutation", engine, spec, str(seeds)], capture_output=True, text=True, cwd=ROOT)
    if proc.returncode != 0:
        return {"error": (proc.stderr.strip().splitlines() or ["no output"])[-1]}
    return json.loads(proc.stdout.strip().splitlines()[-1])


# --- the check -------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Outcome:
    label: str
    caught: str  # "import", "output" or "" (survived)
    detail: str = ""


def check(engine: str, catalog: Catalog, seeds: int = 20, workers: int = 0, only: "list | None" = None) -> "list[Outcome]":
    """Run every mutant of `catalog` (or those whose label starts with one of `only`)."""
    reference = _spawn(engine, None, seeds)
    if "digest" not in reference:
        raise RuntimeError(f"the unmutated provider fails: {reference}")
    todo = [(m, label) for m, label in mutants(catalog) if not only or any(label.startswith(o) for o in only)]
    workers = workers or max(1, (os.cpu_count() or 2) // 2)
    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(lambda ml: _spawn(engine, ml[0], seeds), todo))
    out = []
    for (_, label), r in zip(todo, results):
        if "import" in r:
            out.append(Outcome(label, "import", r["import"]))
        elif "error" in r:
            out.append(Outcome(label, "error", r["error"]))
        else:
            out.append(Outcome(label, "output" if r["digest"] != reference["digest"] else ""))
    return out


def report(outcomes: "list[Outcome]", seeds: int) -> str:
    lines = [f"{'caught by':9s} mutant"]
    for o in sorted(outcomes, key=lambda o: (o.caught != "", o.caught, o.label)):
        lines.append(f"{o.caught or 'SURVIVED':9s} {o.label}" + (f"   [{o.detail}]" if o.detail else ""))
    n = {k: sum(1 for o in outcomes if o.caught == k) for k in ("import", "output", "error", "")}
    lines.append(f"\n{len(outcomes)} mutants over {seeds} seeds x 2 configurations: {n['import']} caught at import, {n['output']} by the generated cases, {n['error']} errors, {n['']} survived")
    return "\n".join(lines)


if __name__ == "__main__":
    spec = json.loads(sys.argv[2])
    print(json.dumps(_run_one(sys.argv[1], Mutant(**spec) if spec else None, int(sys.argv[3]))))

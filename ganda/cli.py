"""The command line: `ganda <engine> <command>`.

    ganda list                              the engines
    ganda orientdb run --iterations 100     a campaign
    ganda orientdb mutate --trials 8        the mutation benchmark
    ganda orientdb probe                    re-check the engine facts the provider rests on
    ganda orientdb doctor                   start a server and answer one query per data model
    ganda orientdb limits                   what the engine can hold, and the large profile on it
    ganda hana rules [--write]              the engine rules as tables (ganda/<engine>/rules/RULES.md)
    ganda orientdb run --scale large        the storage-engine stress mode (large tables,
                                            documents, graphs, strings and binaries)

`run` and `mutate` take one flag per field of the engine's configuration class.

Connection settings come from the environment, or from `ganda/<engine>/.env`
Example at `ganda/<engine>/.env.example`
If env variables are left unset, GANDA pulls the latest Docker image from the provider
"""
from __future__ import annotations

import argparse
import sys

from ganda.core.engine import envfile
from ganda.core.engine.provider import EngineCrash
from ganda.registry import PROVIDERS, provider


def _doctor(engine, args) -> int:
    """The connection layer end to end: a server, one database per data model, one
    trivial query each."""
    try:
        with engine.server(None) as server:
            for data_model in engine.data_models:
                name = engine.database_name("doctor", data_model)
                session = server.fresh_session(name)
                print(f"{engine.name} {data_model}: {name} answers {session.ping()!r}")
                server.drop_database(name)
    finally:
        engine.cleanup()
    return 0


def _limits(engine) -> int:
    """The engine's declared limits (`ganda.core.data.limits`) and what the `large` scale
    profile makes of them on this engine."""
    import dataclasses

    from ganda.core.data import scale as _scale
    from ganda.core.data.generator import limits_of

    limits = limits_of(engine)
    note = "" if engine.limits is not None else "  (the conservative set: this provider declares none yet)"
    print(f"{engine.title}: what the engine can hold{note}")
    for k, v in limits.as_dict().items():
        print(f"  {k:28s} {v:>22,}")
    large = _scale.resolve(engine.config_class(scale="large"), limits)
    defaults = engine.config_class()
    print("the large profile on this engine (`run --scale large`; a flag given explicitly wins):")
    for f in dataclasses.fields(large):
        if getattr(large, f.name) != getattr(defaults, f.name):
            print(f"  --{f.name.replace('_', '-'):28s} {getattr(large, f.name)!s:>12}   (default {getattr(defaults, f.name)})")
    p = _scale.profile("large")
    print(f"  stored state checked by counts and a sample of {p.state_sample} per class; query timeout {p.query_timeout_ms} ms; bundles carry the shape of the data, values cut to {p.max_bundle_value_chars} chars")
    return 0


def _rules(engine, write: bool) -> int:
    """The engine's rule catalog as Markdown tables (`ganda.core.rules.render`); with
    `write`, into `ganda/<engine>/rules/RULES.md`."""
    import pathlib

    from ganda.core.rules.render import render

    if engine.rules is None:
        print(f"{engine.name} has no rule catalog yet")
        return 0
    import importlib

    module = importlib.import_module(f"ganda.{engine.name}.rules")
    text = module.rendered() if hasattr(module, "rendered") else render(engine.rules)
    if not write:
        print(text)
        return 0
    path = pathlib.Path(__file__).resolve().parent / engine.name / "rules" / "RULES.md"
    path.write_text(text)
    print(f"wrote {path}")
    return 0


def _needs_generators(engine, command: str) -> None:
    if not engine.has_generators:
        raise SystemExit(f"{engine.name} is a skeleton provider: it has a connection layer (`ganda {engine.name} doctor`) but no query generators yet, so `{command}` cannot run. See ganda/{engine.name}/PORTING.md.")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "list":
        for name in PROVIDERS:
            engine = provider(name)
            print(f"{name:10s} {engine.title:10s} data models: {', '.join(engine.data_models)}{'' if engine.has_generators else '  (skeleton: connection layer only)'}")
        return 0
    if argv[0] in PROVIDERS:
        envfile.load(argv[0])
    engine = provider(argv[0])
    ap = argparse.ArgumentParser(prog=f"ganda {engine.name}", description=f"{engine.title}: multi-model metamorphic fuzzing")
    sub = ap.add_subparsers(dest="command", required=True)

    from ganda.core.campaign import run as campaign
    from ganda.core.mutation import runner as mutation

    run = sub.add_parser("run", help="a campaign: generate, materialize, query, compare")
    campaign.add_arguments(run, engine)
    run.set_defaults(handler=lambda a: (_needs_generators(engine, "run"), campaign.main(engine, a))[1])
    mutate = sub.add_parser("mutate", help="the mutation benchmark: how often a deliberate corruption is caught")
    mutation.add_arguments(mutate, engine)
    mutate.set_defaults(handler=lambda a: (_needs_generators(engine, "mutate"), mutation.main(engine, a))[1])
    probe = sub.add_parser("probe", help="re-check live the engine facts the provider rests on")
    probe.add_argument("facts", nargs="*", help="only these facts (an id or a prefix); all of them if none given")
    probe.set_defaults(handler=lambda a: engine.probe(a.facts))
    sub.add_parser("doctor", help="start a server and answer one query per data model").set_defaults(handler=lambda a: _doctor(engine, a))
    sub.add_parser("limits", help="what the engine can hold, and what the large scale profile does on it").set_defaults(handler=lambda a: _limits(engine))
    rules = sub.add_parser("rules", help="the engine rules as tables: each answer with its basis and evidence, the divergences, the defects, the error classes")
    rules.add_argument("--write", action="store_true", help="write ganda/<engine>/rules/RULES.md instead of printing")
    rules.set_defaults(handler=lambda a: _rules(engine, a.write))

    args = ap.parse_args(argv[1:])
    try:
        return args.handler(args)
    except EngineCrash as e:
        # no server to talk to (Docker not running, a bad URL): not a traceback's worth
        print(f"ganda {engine.name}: {str(e).splitlines()[0]}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

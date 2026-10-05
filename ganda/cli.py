"""The command line: `ganda <engine> <command>`.

    ganda list                              the engines
    ganda orientdb run --iterations 100     a campaign
    ganda orientdb mutate --trials 8        the mutation benchmark
    ganda orientdb probe                    re-check the engine facts the provider rests on
    ganda orientdb doctor                   start a server and answer one query per data model

`run` and `mutate` take one flag per field of the engine's configuration class.
"""
from __future__ import annotations

import argparse
import sys

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
    engine = provider(argv[0])
    ap = argparse.ArgumentParser(prog=f"ganda {engine.name}", description=f"{engine.title}: multi-model metamorphic fuzzing")
    sub = ap.add_subparsers(dest="command", required=True)

    from ganda.core import campaign
    from ganda.core.mutation import runner as mutation

    run = sub.add_parser("run", help="a campaign: generate, materialize, query, compare")
    campaign.add_arguments(run, engine)
    run.set_defaults(handler=lambda a: (_needs_generators(engine, "run"), campaign.main(engine, a))[1])
    mutate = sub.add_parser("mutate", help="the mutation benchmark: how often a deliberate corruption is caught")
    mutation.add_arguments(mutate, engine)
    mutate.set_defaults(handler=lambda a: (_needs_generators(engine, "mutate"), mutation.main(engine, a))[1])
    sub.add_parser("probe", help="re-check live the engine facts the provider rests on").set_defaults(handler=lambda a: engine.probe())
    sub.add_parser("doctor", help="start a server and answer one query per data model").set_defaults(handler=lambda a: _doctor(engine, a))
    args = ap.parse_args(argv[1:])
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())

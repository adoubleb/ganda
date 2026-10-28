"""Code coverage of the engine under a campaign: the hook protocol.

A provider that can measure its engine returns a `Collector` from `Provider.coverage`;
the campaign calls its hooks at the points below and never looks inside. What is
measured, and how (a JVM agent, an instrumented build), is the provider's business.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

# `--coverage` levels: off; seed = one window per seed (the growth curve); case /
# variant = also a window per case / per variant (which generator hit what)
LEVELS = ("off", "seed", "case", "variant")


class Collector:
    """The hooks a campaign calls; every one may be a no-op."""

    def after_setup(self) -> None:
        """Every data model of the seed has been materialized."""

    def after_case(self, case) -> None:
        """Every variant of `case` has been asked."""

    def after_variant(self, case, variant: str) -> None:
        """One variant of `case` has been asked."""

    def before_restart(self) -> None:
        """The server crashed and is about to be restarted (its counters are lost)."""

    def after_seed(self, seed: int) -> None:
        """The seed is done."""

    def summary(self) -> "dict | None":
        """What the campaign statistics record."""
        return None

    def write(self, stats_dir: str, tag: str) -> None:
        """Persist the raw coverage data."""

    def record(self, summary: dict, stats_dir: str, tag: str) -> dict:
        """Write the full summary; the part the campaign's stats file keeps."""
        return summary

    def print_summary(self, summary: dict) -> None:
        pass


def window_log_path(args) -> "str | None":
    """`<stats dir>/<tag>-windows.jsonl.gz` under `--coverage variant`
    (`ganda.core.engine.window_log`), a fresh file."""
    if getattr(args, "coverage", "off") != "variant":
        return None
    os.makedirs(args.stats_dir, exist_ok=True)
    path = os.path.join(args.stats_dir, f"{args.run_tag}-windows.jsonl.gz")
    if os.path.exists(path):
        os.remove(path)
    return path


@contextmanager
def measured_server(engine, args):
    """`engine.server(args)` with its collector attached: yields (server, collector or
    None) and persists the coverage once the server is down."""
    if args.coverage in ("case", "variant") and args.workers > 1:
        raise SystemExit(f"--coverage {args.coverage} attributes each window to what ran since the last dump: it needs --workers 1")
    with engine.server(args) as server:
        collector = engine.coverage(server, args)
        yield server, collector
    if collector:
        collector.write(args.stats_dir, args.run_tag)

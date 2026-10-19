"""Code coverage of the engine under a campaign: the hook protocol.

A provider that can measure its engine returns a `Collector` from `Provider.coverage`;
the campaign calls its hooks at the points below and never looks inside. What is
measured, and how (a JVM agent, an instrumented build), is the provider's business.
"""
from __future__ import annotations

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

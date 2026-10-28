"""Metamorphic coverage (MC) of a campaign: the code the variants of a case reach
DIFFERENTLY (Ba, Jiang and Rigger, "Metamorphic Coverage", ISSTA 2026).

The paper defines MC over a pair of test inputs: the symmetric difference of their
coverage sets. A Ganda case has N variants, so MC here is N-way: an element (a probe, a
counter, a line) is in a case's MC when some of its compared variants reach it and some
do not (0 < k < N). Over the variants of a connected comparison graph that is exactly
the union of the pairwise symmetric differences the paper would take over every pair,
so the headline number is the paper's; the per-pair breakdown is kept beside it.

A coverage set is a dict {unit: int}, bit i of the int being element i of the unit (a
class's probes, a module's counters) -- whatever the collector measures. The campaign
MC is the union of the cases' MC; `background` (what the server reached in windows in
which nothing was asked: timers, housekeeping) is reported apart and taken out.
"""
from __future__ import annotations

import itertools
import time

from ganda.core.oracle.case import comparable_pairs, data_model_of
from ganda.core.oracle.oracle import REPEAT


def _or(into: dict, cov: dict) -> None:
    for unit, mask in cov.items():
        if mask:
            into[unit] = into.get(unit, 0) | mask


def _count(cov: dict) -> int:
    return sum(m.bit_count() for m in cov.values())


def _minus(cov: dict, other: dict) -> dict:
    out = {}
    for unit, mask in cov.items():
        rest = mask & ~other.get(unit, 0)
        if rest:
            out[unit] = rest
    return out


def split(windows: dict) -> "tuple[dict, dict]":
    """(reached by some variant, reached by some but not all) of {variant: coverage}."""
    union: dict = {}
    for cov in windows.values():
        _or(union, cov)
    out = {}
    for unit, any_mask in union.items():
        every = any_mask
        for cov in windows.values():
            every &= cov.get(unit, 0)
            if not every:
                break
        rest = any_mask & ~every
        if rest:
            out[unit] = rest
    return union, out


def idle_windows(dump, seconds: float = 5.0, every: float = 0.5) -> dict:
    """What the server reaches while nothing is asked: `dump()` (a reset dump, as a
    coverage set) after each of `seconds / every` quiet intervals, OR-ed together."""
    dump()
    out: dict = {}
    for _ in range(max(1, int(seconds / every))):
        time.sleep(every)
        _or(out, dump())
    return out


def pair_key(a: str, b: str) -> str:
    return "~".join(sorted((data_model_of(a), data_model_of(b))))


class MetamorphicCoverage:
    """Accumulates the N-way MC of every case the campaign measured."""

    def __init__(self):
        self.covered: dict = {}  # what the compared variants reached
        self.mc: dict = {}  # the union of the cases' MC
        self.background: dict = {}
        self.repeat_noise: dict = {}  # what a variant reached once and not on its repeat, or back
        self.repeats = 0
        self.cases = 0
        self.zero_cases = 0  # cases whose variants all reached the same code
        self.case_mc_total = 0
        self.variant_counts: dict = {}
        self.generators: dict = {}  # generator -> {"mc": cov, "covered": cov, "cases", "zero"}
        self.pairs: dict = {}  # "doc~graph" -> {"mc": cov, "pairs": n}

    def add_background(self, cov: dict) -> None:
        _or(self.background, cov)

    def add_case(self, case, windows: dict) -> "int | None":
        """`windows`: {variant: coverage} of the variants that ran (and of their
        repeats, `<variant>#again`, which only measure the noise). Only the variants in
        a compared pair count; a case with fewer than two of them has no relation to
        measure (None)."""
        for v, cov in windows.items():
            again = windows.get(v + REPEAT)
            if again is not None:
                self.repeats += 1
                _or(self.repeat_noise, {u: cov.get(u, 0) ^ again.get(u, 0) for u in cov.keys() | again.keys()})
        windows = {v: cov for v, cov in windows.items() if not v.endswith(REPEAT)}
        pairs = comparable_pairs(case, list(windows))
        members = sorted({v for p in pairs for v in p})
        if len(members) < 2:
            return None
        case_windows = {v: windows[v] for v in members}
        union, case_mc = split(case_windows)
        size = _count(_minus(case_mc, self.background))
        self.cases += 1
        self.case_mc_total += size
        self.zero_cases += size == 0
        self.variant_counts[len(members)] = self.variant_counts.get(len(members), 0) + 1
        _or(self.covered, union)
        _or(self.mc, case_mc)
        g = self.generators.setdefault(case.generator_name, {"mc": {}, "covered": {}, "cases": 0, "zero": 0})
        _or(g["mc"], case_mc)
        _or(g["covered"], union)
        g["cases"] += 1
        g["zero"] += size == 0
        for a, b in pairs:
            p = self.pairs.setdefault(pair_key(a, b), {"mc": {}, "pairs": 0})
            p["pairs"] += 1
            left, right = case_windows[a], case_windows[b]
            _or(p["mc"], {u: left.get(u, 0) ^ right.get(u, 0) for u in left.keys() | right.keys()})
        return size

    def summary(self, unit_name=None, denominator: "int | None" = None) -> dict:
        """`unit_name(unit) -> str` groups the MC by module for the breakdown;
        `denominator` is every element the build has, when the collector knows it."""
        bg = self.background
        mc = _minus(self.mc, bg)
        covered = _minus(self.covered, bg)
        n_mc, n_cov = _count(mc), _count(covered)
        out = {
            "cases": self.cases,
            "variants_per_case": dict(sorted(self.variant_counts.items())),
            "mc": n_mc,
            "covered": n_cov,
            "mc_of_covered": round(n_mc / n_cov, 4) if n_cov else 0.0,
            "mc_raw": _count(self.mc),
            "background": _count(bg),
            "background_in_mc": _count(self.mc) - n_mc,
            "mean_case_mc": round(self.case_mc_total / self.cases, 1) if self.cases else 0.0,
            "zero_mc_cases": self.zero_cases,
        }
        if self.repeats:
            noise = _minus(self.repeat_noise, bg)
            out["repeats"] = self.repeats
            out["repeat_noise"] = _count(noise)
            out["mc_less_repeat_noise"] = _count(_minus(mc, noise))
        if denominator:
            out["elements"] = denominator
            out["mc_of_elements"] = round(n_mc / denominator, 5)
            out["covered_of_elements"] = round(n_cov / denominator, 5)
        out["generators"] = {
            name: {"mc": _count(_minus(g["mc"], bg)), "covered": _count(_minus(g["covered"], bg)), "cases": g["cases"], "zero_mc_cases": g["zero"]}
            for name, g in sorted(self.generators.items())
        }
        out["data_model_pairs"] = {
            key: {"mc": _count(_minus(p["mc"], bg)), "pairs": p["pairs"]}
            for key, p in sorted(self.pairs.items(), key=lambda kv: -_count(kv[1]["mc"]))
        }
        if unit_name is not None:
            by: dict = {}
            for unit, mask in mc.items():
                row = by.setdefault(unit_name(unit), [0, 0])
                row[0] += mask.bit_count()
            for unit, mask in covered.items():
                row = by.setdefault(unit_name(unit), [0, 0])
                row[1] += mask.bit_count()
            ranked = sorted(by.items(), key=lambda kv: -kv[1][0])
            out["modules"] = {name: {"mc": m, "covered": c} for name, (m, c) in itertools.islice(ranked, 40)}
        return out


# what a campaign's statistics file keeps of the summary
HEADLINE = ("cases", "mc", "covered", "mc_of_covered", "mean_case_mc", "zero_mc_cases", "background")


def print_summary(s: dict, top: int = 10) -> None:
    pct = f"{100 * s['mc_of_elements']:.2f}% of {s['elements']} elements, " if "elements" in s else ""
    print(f"metamorphic coverage: {s['mc']} elements ({pct}{100 * s['mc_of_covered']:.1f}% of the {s['covered']} covered) over {s['cases']} cases; "
          f"mean per case {s['mean_case_mc']}, zero-MC cases {s['zero_mc_cases']}, background {s['background']} ({s['background_in_mc']} taken out of MC)")
    for key, row in list(s["data_model_pairs"].items())[:top]:
        print(f"  pair {key:24s} mc={row['mc']:7d} pairs={row['pairs']}")
    for name, row in s["generators"].items():
        print(f"  {name:26s} mc={row['mc']:7d} covered={row['covered']:7d} cases={row['cases']:5d} zero={row['zero_mc_cases']}")
    for name, row in list(s.get("modules", {}).items())[:top]:
        print(f"  module {name:48s} mc={row['mc']:6d} covered={row['covered']:7d}")

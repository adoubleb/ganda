from __future__ import annotations

from enum import StrEnum


class On(StrEnum):
    """Which record of a question a predicate tests."""

    SOURCE = "source"
    EDGE = "edge"
    TARGET = "target"

    __repr__ = str.__repr__


class PathDirection(StrEnum):
    """Which way a shortest path may follow the edges."""

    OUT = "OUT"
    IN = "IN"
    BOTH = "BOTH"

    __repr__ = str.__repr__


class Reach(StrEnum):
    """How the goal of a shortest-path question sits relative to its start and bound."""

    SAME = "same"  # the goal is the start
    YES = "yes"  # reachable within the bound
    CUT = "cut"  # reachable, but beyond the bound
    NO = "no"  # unreachable

    __repr__ = str.__repr__


class NullOp(StrEnum):
    """The null tests: they bind no pivot (`IS [NOT] DEFINED` tells an absent field from a null)."""

    IS_NULL = "IS NULL"
    IS_NOT_NULL = "IS NOT NULL"
    IS_DEFINED = "IS DEFINED"
    IS_NOT_DEFINED = "IS NOT DEFINED"

    __repr__ = str.__repr__


class MapOp(StrEnum):
    """The map operators: `m CONTAINSKEY :k`, `m.values() CONTAINS :p`, `m CONTAINSVALUE :p`."""

    CONTAINSKEY = "CONTAINSKEY"
    VALUES_CONTAINS = "VALUES CONTAINS"
    CONTAINSVALUE = "CONTAINSVALUE"

    __repr__ = str.__repr__


class CompoundOp(StrEnum):
    """How a `Compound` joins its children, or negates its one child."""

    AND = "AND"
    OR = "OR"
    NOT = "NOT"

    __repr__ = str.__repr__

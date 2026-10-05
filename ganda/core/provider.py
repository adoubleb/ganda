"""The contract between the engine-agnostic core and one DBMS.

A `Provider` is everything the core needs to know about an engine. The core never
imports a provider package: it reaches the engine through the provider object a schema
was generated for (`Schema.engine`) or one passed to it explicitly.
"""
from __future__ import annotations

from typing import Any


class EngineError(RuntimeError):
    """The engine rejected a statement. `internal` marks an answer that is a defect in
    itself (a raw exception from inside the engine), whatever the other variants did."""

    def __init__(self, message: str, internal: bool = False, status: int = 0):
        super().__init__(message)
        self.internal = internal
        self.status = status


class EngineCrash(RuntimeError):
    """The server stopped answering (a dead process, a transport failure)."""


class Semantics:
    """The engine's value semantics: what the reference interpreter must reproduce to
    predict an answer from the `Instance`."""

    def key(self, v: Any):
        """A total-order key reproducing the engine's comparison inside one type."""
        raise NotImplementedError

    def eq(self, a: Any, b: Any) -> bool:
        raise NotImplementedError

    def lt(self, a: Any, b: Any) -> bool:
        raise NotImplementedError

    def predicate_hit(self, present: bool, v: Any, op: str, pivot: Any, ftype: "str | None" = None, ci: bool = False, key: "str | None" = None) -> bool:
        """Whether a stored value (absent when not `present`) satisfies `op pivot`."""
        raise NotImplementedError

    def evaluate(self, values: dict, p) -> bool:
        """Whether a record satisfies a predicate tree (the engine's boolean logic)."""
        raise NotImplementedError

    def apply_arith(self, v: Any, sym: str, c: Any, ftype: "str | None"):
        """`v <sym> c` as the engine computes it."""
        raise NotImplementedError

    def agg(self, fn: str, values: list, ftype: "str | None" = None):
        raise NotImplementedError

    def mean(self, values: list):
        raise NotImplementedError

    def sum_is_exact(self, values: list, ftype: "str | None" = "int") -> bool:
        """Whether the engine's sum of `values` is exact in every order."""
        raise NotImplementedError

    def aggregate_over_no_rows(self, fn: str) -> list:
        """The rows an aggregate other than a count answers over no input rows."""
        raise NotImplementedError

    def nulls_first(self, desc: bool) -> bool:
        """Whether an ORDER BY puts the null keys first in this direction."""
        raise NotImplementedError

    def order_key(self, v: Any, desc: bool):
        """A per-key sort key for an ORDER BY: `key` with the engine's null placement,
        reversed for a descending key."""
        raise NotImplementedError

    def orders_exactly(self, ftype, custom_keys: bool) -> bool:
        """Whether the reference reproduces an id-tiebroken ORDER BY on a column of
        `ftype` (a collation the reference does not model makes it inexact)."""
        raise NotImplementedError

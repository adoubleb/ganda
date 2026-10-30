from __future__ import annotations

from dataclasses import dataclass

# the enclosing row of a correlated subquery
CURRENT = "$parent.$current."


@dataclass(frozen=True)
class Path:
    prefix: str = ""
    identity: str = "id"
    # a list element: its definedness tests read its keys (`predicate.render_predicate`)
    element: bool = False

    def field(self, f: str) -> str:
        return self.prefix + f

    @property
    def id(self) -> str:
        return self.prefix + self.identity


@dataclass(frozen=True)
class Renamed:
    name: str = "v"

    def field(self, f: str) -> str:
        return self.name

    @property
    def id(self) -> str:
        return self.name


@dataclass(frozen=True)
class Semijoin:
    column: str
    table: str

    def condition(self, text: str) -> "tuple[str, tuple]":
        return f"{self.column} IN (SELECT id FROM {self.table} WHERE {text})", ()


@dataclass(frozen=True)
class Lookup:
    table: str
    key: str
    k: int = 0

    def condition(self, text: str) -> "tuple[str, tuple]":
        return f"$n{self.k}.size() > 0", (f"$n{self.k} = (SELECT FROM {self.table} WHERE id = {self.key} AND {text})",)

    def value(self, f: str) -> "tuple[str, tuple]":
        # `[0]`: the lookup by id yields at most one record, and indexing it keeps the
        # projection flat (a `$f.v` list-of-lists is a ClassCastException under an
        # aggregate, probed)
        return f"$f{self.k}[0].v", (f"$f{self.k} = (SELECT {f} AS v FROM {self.table} WHERE id = {self.key})",)


RECORD = Path()
RENAMED = Renamed()

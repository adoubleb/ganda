"""The scope of a rendering: the text that stands for a record the IR names -- the
source record, a hop's target or edge, a select's or a write's record, the record a
correlated subquery is asked for -- at one position of a frame (`frames.py`: the frame
is the text every data model shares, the model-specific part the slot a data model
fills, the scope what the IR's names are there).

    ref        stands for                                    e.g.
    Path       a record reached by a prefix: its field `f`   `f` (the current record, a
               is `<prefix>f`, its id `<prefix><identity>`   bracket element, a MATCH
                                                             alias's where), `out.f` /
                                                             `in.f` (an edge's ends),
                                                             `e[k].f` (a link element,
                                                             presence through its keys),
                                                             `$parent.$current.@rid`
    Renamed    a subquery row that projected its value as    `$s.v`
               `v`: its id and its field both read `v`
    Semijoin   a record the text names by an id column: a    `dst_id IN (SELECT id FROM n2
               predicate on it is a semi-join                WHERE p)`
    Lookup     the same record looked up per row in a LET:   `$n0.size() > 0`, `$f0[0].v`
               a predicate is `$n<k>.size() > 0`, a field
               `$f<k>[0].v`

`value(f)` and `condition(text)` answer (text, lets): the lets are the LET bindings the
text needs, which the frame puts in its LET slot. A predicate on a Semijoin or a Lookup
is rendered against the plain record (inside the subquery the record is the current
one) and handed over as text (`predicate.condition`); a Path renders the predicate
itself, since its prefix reaches every leaf.
"""
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

    def value(self, f: str) -> "tuple[str, tuple]":
        return self.field(f), ()


@dataclass(frozen=True)
class Renamed:
    name: str = "v"

    def field(self, f: str) -> str:
        return self.name

    @property
    def id(self) -> str:
        return self.name

    def value(self, f: str) -> "tuple[str, tuple]":
        return self.name, ()


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

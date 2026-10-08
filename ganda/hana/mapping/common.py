"""The setup steps of SAP HANA's data models and what the three mappings share.

A step is a plain dataclass with a JSON `to_dict()`, so a bug bundle records the exact
plan that ran (`ganda.core.setup_plan`); its op name is prefixed `hana_` (the step
registry is shared by every provider):

    Sql(text, params)          one SQL statement; `:name` parameters (the session binds
                               them positionally)
    SqlBatch(text, rows)       one prepared statement over many parameter rows (the
                               driver's `executemany`)
    Sparql(text)               one SPARQL update; the text names the database's graph
                               as `dialect.GRAPH_TOKEN`
    Transaction(steps, commit) the steps in one transaction, committed or rolled back

The values a step carries are the model's (`dialect` says how each data model stores
them): a LIST travels as its canonical JSON text to a column and as one row per element
to its child table.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ganda.core.data_generator import initial_node_values, initial_nodes  # noqa: F401
from ganda.core.setup_plan import initial_edge_values, initial_edges, step, step_from_dict  # noqa: F401


@step("hana_declare")
@dataclass
class Declare:
    """No statement: the declared type of every column of the data model ({table:
    {field: FieldType.value}}), which the session keeps to read a stored value back
    in the model's type (a LIST column holds JSON text)."""

    tables: dict

    def to_dict(self) -> dict:
        return {"op": "hana_declare", "tables": self.tables}

    @classmethod
    def from_dict(cls, d: dict) -> "Declare":
        return cls(d["tables"])


@step("hana_sql")
@dataclass
class Sql:
    text: str
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"op": "hana_sql", "text": self.text, "params": self.params}

    @classmethod
    def from_dict(cls, d: dict) -> "Sql":
        return cls(d["text"], d.get("params") or {})


@step("hana_sql_batch")
@dataclass
class SqlBatch:
    text: str
    rows: list = field(default_factory=list)  # [dict]: one parameter set per execution

    def to_dict(self) -> dict:
        return {"op": "hana_sql_batch", "text": self.text, "rows": self.rows}

    @classmethod
    def from_dict(cls, d: dict) -> "SqlBatch":
        return cls(d["text"], d.get("rows") or [])


@step("hana_sparql")
@dataclass
class Sparql:
    text: str

    def to_dict(self) -> dict:
        return {"op": "hana_sparql", "text": self.text}

    @classmethod
    def from_dict(cls, d: dict) -> "Sparql":
        return cls(d["text"])


@step("hana_transaction")
@dataclass
class Transaction:
    steps: list
    commit: bool = True

    def to_dict(self) -> dict:
        return {"op": "hana_transaction", "commit": self.commit, "steps": [s.to_dict() for s in self.steps]}

    @classmethod
    def from_dict(cls, d: dict) -> "Transaction":
        return cls([step_from_dict(s) for s in d["steps"]], d.get("commit", True))


def wrap(data: list, form) -> list:
    """The data steps through the form's transaction door."""
    if form.transaction == "tx" and data:
        return [Transaction(data, True)]
    return data


def chunks(items: list, size: int) -> list:
    size = max(1, size)
    return [items[i : i + size] for i in range(0, len(items), size)]

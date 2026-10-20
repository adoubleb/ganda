"""The setup steps of SAP HANA's data models and what the three mappings share.

A step is a plain dataclass with a JSON `to_dict()`, so a bug bundle records the exact
plan that ran (`ganda.core.engine.setup_plan`); its op name is prefixed `hana_` (the step
registry is shared by every provider):

    Sql(text, params)          one SQL statement; `:name` parameters (the session binds
                               them positionally)
    SqlBatch(text, rows)       one prepared statement over many parameter rows (the
                               driver's `executemany`)
    Sparql(text)               one SPARQL update; the text names the database's graph
                               as `dialect.GRAPH_TOKEN`
    Transaction(steps, commit) the steps in one transaction, committed or rolled back
    GraphWorkspace(...)        the graph data model's CREATE GRAPH WORKSPACE, as data:
                               its views, labels, keys and attribute names

The values a step carries are the model's (`dialect` says how each data model stores
them): a LIST travels as its canonical JSON text to a column and as one row per element
to its child table.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ganda.core.data.generator import initial_node_values, initial_nodes  # noqa: F401
from ganda.core.engine.setup_plan import initial_edge_values, initial_edges, step, step_from_dict  # noqa: F401


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


@step("hana_graph_workspace")
@dataclass
class GraphWorkspace:
    """`CREATE GRAPH WORKSPACE`: the graph data model's workspace over the views of its
    tables. `vertices` and `edges` hold one dict per view -- `table` (the view), `label`,
    `key`, for an edge `source` / `target` and the vertex views they reference
    (`source_table` / `target_table`), and `columns`, the `[physical column, attribute]`
    pairs the view projects -- so a bundle records the definition and a session knows
    the attribute names without the schema. `cache` is one of
    `dialect.GRAPH_CACHE_FORMS`."""

    name: str
    vertices: list
    edges: list
    cache: str = "dynamic"

    def sql(self) -> str:
        parts = [f"CREATE GRAPH WORKSPACE {self.name}"]
        for v in self.vertices:
            parts.append(f"VERTEX TABLE {v['table']} KEY COLUMN {v['key']} LABEL '{v['label']}'")
        for e in self.edges:
            parts.append(f"EDGE TABLE {e['table']} KEY COLUMN {e['key']} SOURCE COLUMN {e['source']} REFERENCES {e['source_table']} TARGET COLUMN {e['target']} REFERENCES {e['target_table']} LABEL '{e['label']}'")
        return " ".join(parts) + (" NO CACHE" if self.cache == "none" else "")

    def vertex(self, label: str) -> dict:
        return next(v for v in self.vertices if v["label"] == label)

    def edge(self, label: str) -> dict:
        return next(e for e in self.edges if e["label"] == label)

    def to_dict(self) -> dict:
        return {"op": "hana_graph_workspace", "name": self.name, "vertices": self.vertices, "edges": self.edges, "cache": self.cache}

    @classmethod
    def from_dict(cls, d: dict) -> "GraphWorkspace":
        return cls(d["name"], d["vertices"], d["edges"], d.get("cache", "dynamic"))


def wrap(data: list, form) -> list:
    """The data steps through the form's transaction door."""
    if form.transaction == "tx" and data:
        return [Transaction(data, True)]
    return data


def chunks(items: list, size: int) -> list:
    size = max(1, size)
    return [items[i : i + size] for i in range(0, len(items), size)]

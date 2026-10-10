"""The graph data model: SAP HANA's Property Graph Engine over the rel data model's tables.

    n1, e1, n1__f3                          the column tables of `mapping.relational`, verbatim
    gv_n1(id, f1 AS n1_f1, ...)             one VIEW per node table and per junction table:
    gv_e1(eid, src_id, dst_id, w AS e1_w)   the columns under the workspace's attribute names
    GRAPH WORKSPACE gw VERTEX TABLE gv_n1 KEY COLUMN id LABEL 'n1' ...
                       EDGE TABLE gv_e1 KEY COLUMN eid SOURCE COLUMN src_id REFERENCES gv_n1
                                        TARGET COLUMN dst_id REFERENCES gv_n2 LABEL 'e1' ...
                       [NO CACHE]

The workspace reads the tables through views because it wants ONE type per attribute
name across its vertex tables (and across its edge tables), and a class's own fields
`f1`, `f2`, ... are drawn per class: the view qualifies them `<table>_<field>`
(`dialect.graph_attr`); the keys and a base-class field keep their names. A view-backed
graph table is not checked for the constraints a table-backed one needs (NOT NULL and
UNIQUE on the key, NOT NULL on the endpoints); the tables hold them anyway. Every edge
table names the vertex views its endpoints reference, as a workspace of several vertex
tables must.

The data is the relational setup's (`build_relational_setup`, under the write form drawn
for `graph`): the graph has no write of its own -- any change to the tables is a change
to the graph -- so the inserts, the churn replay, the cancelled junk transaction and the
delta merge are the rel data model's, and the workspace reads them through its dynamic
cache, or no cache at all (the `graph_cache` axis). The views and the workspace are
created with the DDL, before the data.
"""
from __future__ import annotations

from ganda.core.model import Instance, Schema
from ganda.hana import physical_design
from ganda.hana.dialect import GRAPH_WORKSPACE, graph_columns, graph_label, graph_view
from ganda.hana.mapping.common import GraphWorkspace, Sql
from ganda.hana.mapping.relational import build_relational_setup

DATA_MODEL = "graph"


def _tables(schema: Schema) -> list:
    return [nt.name for nt in schema.node_types] + [et.name for et in schema.edge_types]


def view_ddl(schema: Schema) -> list:
    """One view per node table and per junction table, the columns renamed to the
    workspace's attributes (lowercase in the unquoted DDL; the catalog uppercases)."""
    steps = []
    for t in _tables(schema):
        cols = ", ".join(c if c == a.lower() else f"{c} AS {a.lower()}" for c, a in graph_columns(schema, t))
        steps.append(Sql(f"CREATE VIEW {graph_view(t)} AS SELECT {cols} FROM {t}"))
    return steps


def workspace(schema: Schema, cache: str = "dynamic") -> GraphWorkspace:
    vertices = [
        {"table": graph_view(nt.name), "label": graph_label(nt.name), "key": "id", "columns": [list(x) for x in graph_columns(schema, nt.name)]}
        for nt in schema.node_types
    ]
    edges = [
        {
            "table": graph_view(et.name),
            "label": graph_label(et.name),
            "key": "eid",
            "source": "src_id",
            "source_table": graph_view(et.from_type),
            "target": "dst_id",
            "target_table": graph_view(et.to_type),
            "columns": [list(x) for x in graph_columns(schema, et.name)],
        }
        for et in schema.edge_types
    ]
    return GraphWorkspace(GRAPH_WORKSPACE, vertices, edges, cache)


def graph_ddl(instance: Instance) -> list:
    """The views, then the workspace over them."""
    schema = instance.schema
    return view_ddl(schema) + [workspace(schema, physical_design.graph_cache(schema))]


def build_graph_setup(instance: Instance) -> list:
    return build_relational_setup(instance, DATA_MODEL, extra_ddl=graph_ddl(instance))

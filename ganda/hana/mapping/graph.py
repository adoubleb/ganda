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
from ganda.hana.dialect import GRAPH_WORKSPACE, SCHEMA_TOKEN, graph_columns, graph_label, graph_view
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


SCRIPT_VERTICES, SCRIPT_EDGES, SCRIPT_WORKSPACE = "gs_v", "gs_e", "gws"
# the GraphScript procedures (`concrete_syntax/path.py`, the `script` syntax): a shortest
# path over the whole graph, and over the subgraph of one edge KIND. Every variable is
# `sv_*` (`connection.positional` binds any other `:name`); the schema is the session's
# (`dialect.SCHEMA_TOKEN`, replaced by `connection._bind_graph`)
_PATH_BODY = 'VERTEX sv_va = Vertex(:{g}, :sv_ia); VERTEX sv_vb = Vertex(:{g}, :sv_ib); WeightedPath<BIGINT> sv_p = Shortest_Path(:{g}, :sv_va, :sv_vb, :sv_dir); sv_o = SELECT :sv_v."ID" FOREACH sv_v IN Vertices(:sv_p);'
SCRIPT_PROCEDURES = (
    f'CREATE PROCEDURE p_gs_path (IN sv_ia NVARCHAR(256), IN sv_ib NVARCHAR(256), IN sv_dir NVARCHAR(16), OUT sv_o TABLE (ID NVARCHAR(256))) LANGUAGE GRAPH READS SQL DATA AS BEGIN GRAPH sv_g = Graph("{SCHEMA_TOKEN}", "{SCRIPT_WORKSPACE.upper()}"); {_PATH_BODY.format(g="sv_g")} END',
    f'CREATE PROCEDURE p_gs_path_kind (IN sv_ia NVARCHAR(256), IN sv_ib NVARCHAR(256), IN sv_kind NVARCHAR(256), IN sv_dir NVARCHAR(16), OUT sv_o TABLE (ID NVARCHAR(256))) LANGUAGE GRAPH READS SQL DATA AS BEGIN GRAPH sv_g = Graph("{SCHEMA_TOKEN}", "{SCRIPT_WORKSPACE.upper()}"); GRAPH sv_sg = SubGraph(:sv_g, sv_e IN Edges(:sv_g) WHERE :sv_e."KIND" == :sv_kind); {_PATH_BODY.format(g="sv_sg")} END',
)


def script_ddl(schema: Schema) -> list:
    """GraphScript's own workspace (probe G35: GraphScript compiles over a HOMOGENEOUS
    workspace without labels only): one view uniting every node table's key with its
    table name as KIND, one uniting every junction table's key and ends with its KIND, the
    workspace `gws` over the two (keys only: the path and reach questions need no
    attribute), and the procedures. Plain SQL steps: the offline stand-in skips the
    workspace and the procedures (its translator keeps `gw`)."""
    v = " UNION ALL ".join(f"SELECT id, CAST('{nt.name}' AS NVARCHAR(256)) AS kind FROM {nt.name}" for nt in schema.node_types)
    e = " UNION ALL ".join(f"SELECT eid, src_id, dst_id, CAST('{et.name}' AS NVARCHAR(256)) AS kind FROM {et.name}" for et in schema.edge_types)
    steps = [
        Sql(f"CREATE VIEW {SCRIPT_VERTICES} AS {v}"),
        Sql(f"CREATE VIEW {SCRIPT_EDGES} AS {e}"),
        Sql(f"CREATE GRAPH WORKSPACE {SCRIPT_WORKSPACE} VERTEX TABLE {SCRIPT_VERTICES} KEY COLUMN id EDGE TABLE {SCRIPT_EDGES} KEY COLUMN eid SOURCE COLUMN src_id TARGET COLUMN dst_id"),
    ]
    return steps + [Sql(text) for text in SCRIPT_PROCEDURES]


def graph_ddl(instance: Instance) -> list:
    """The views, then the workspace over them, then GraphScript's workspace and procedures."""
    schema = instance.schema
    return view_ddl(schema) + [workspace(schema, physical_design.graph_cache(schema))] + script_ddl(schema)


def build_graph_setup(instance: Instance) -> list:
    return build_relational_setup(instance, DATA_MODEL, extra_ddl=graph_ddl(instance))

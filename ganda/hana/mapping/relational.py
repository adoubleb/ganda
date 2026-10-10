"""The relational data model: column tables.

    n1(id NVARCHAR(256) PRIMARY KEY, f1 BIGINT, f2 DOUBLE, f3 NVARCHAR(5000), ...)
    n1__f3(owner NVARCHAR(256), pos INTEGER, v BIGINT)        -- a LIST column's elements
    e1(eid NVARCHAR(256) PRIMARY KEY, src_id NVARCHAR(256), dst_id NVARCHAR(256), w ...)

Every edge type is a JUNCTION table with the endpoints' logical ids (an index on each,
as every relational schema has on the column a join goes through), so a hop is a join.
A LIST column holds the list's canonical JSON text and its child table one row per
element (`dialect`). The `id` index is the PRIMARY KEY unless the id index axis drew
another kind (a unique or plain index, or none); a class may be HASH-partitioned on
`id` (the partition axis).

The setup writes the INITIAL records (a churned node with its first values, a churned
edge before its delete), then replays the churn in order through the form's update
door (`UPDATE` / `UPSERT ... WHERE` / `MERGE INTO`), deletes a churned node's junction
rows before the node, and may end with a cancelled transaction of junk rows and a
delta merge of every table (`MERGE DELTA OF`: the rows leave the write-optimized delta
store for the main store, so the queries read the other storage path).

The graph data model's tables are these too (`mapping.graph`): `ddl` and
`build_relational_setup` take the data model whose write form and indexes apply, and
the DDL the graph adds (its views and workspace).
"""
from __future__ import annotations

from ganda.core.model import EdgeDelete, EdgeUpdate, FieldType, Instance, NodeDelete, NodeUpdate
from ganda.hana.dialect import ID_TYPE, SQL_TYPES, child_table, list_text
from ganda.hana.mapping.common import Declare, Sql, SqlBatch, Transaction, initial_edge_values, initial_edges, initial_node_values, initial_nodes, wrap

DATA_MODEL = "rel"


def declared_types(instance: Instance) -> dict:
    """{table: {column: FieldType.value}} of every node class and junction table."""
    schema = instance.schema
    out = {nt.name: {f.name: f.type.value for f in schema.query_fields(nt.name)} for nt in schema.node_types}
    for et in schema.edge_types:
        out[et.name] = {f.name: f.type.value for f in et.fields}
    return out


def _column_defs(fields) -> list:
    return [f"{f.name} {SQL_TYPES[f.type]}" for f in fields]


def _child_ddl(table: str, f) -> list:
    t = child_table(table, f.name)
    return [Sql(f"CREATE COLUMN TABLE {t} (owner {ID_TYPE} NOT NULL, pos INTEGER NOT NULL, v BIGINT)"), Sql(f"CREATE INDEX ix_{t}_owner ON {t} (owner)")]


def _index_ddl(ix) -> Sql:
    cols = ", ".join(ix.all_fields)
    if ix.kind == "UNIQUE":
        return Sql(f"CREATE UNIQUE INDEX {ix.name()} ON {ix.table} ({cols})")
    return Sql(f"CREATE INDEX {ix.name()} ON {ix.table} ({cols})")


def ddl(instance: Instance, data_model: str = DATA_MODEL) -> list:
    schema = instance.schema
    kind = schema.id_index_kind(data_model)
    steps = [Declare(declared_types(instance))]
    for nt in schema.node_types:
        fields = schema.query_fields(nt.name)
        cols = [f"id {ID_TYPE}" + (" PRIMARY KEY" if kind == "UNIQUE" else " NOT NULL")] + _column_defs(fields)
        part = f" PARTITION BY HASH (id) PARTITIONS {nt.clusters}" if nt.clusters > 1 else ""
        steps.append(Sql(f"CREATE COLUMN TABLE {nt.name} ({', '.join(cols)}){part}"))
        if kind == "UNIQUE_INDEX":
            steps.append(Sql(f"CREATE UNIQUE INDEX ix_{nt.name}_id ON {nt.name} (id)"))
        elif kind == "INDEX":
            steps.append(Sql(f"CREATE INDEX ix_{nt.name}_id ON {nt.name} (id)"))
        for f in fields:
            if f.type is FieldType.LIST:
                steps += _child_ddl(nt.name, f)
    for et in schema.edge_types:
        cols = [f"eid {ID_TYPE} PRIMARY KEY", f"src_id {ID_TYPE} NOT NULL", f"dst_id {ID_TYPE} NOT NULL"] + _column_defs(et.fields)
        steps.append(Sql(f"CREATE COLUMN TABLE {et.name} ({', '.join(cols)})"))
        steps.append(Sql(f"CREATE INDEX ix_{et.name}_src ON {et.name} (src_id)"))
        steps.append(Sql(f"CREATE INDEX ix_{et.name}_dst ON {et.name} (dst_id)"))
        for f in et.fields:
            if f.type is FieldType.LIST:
                steps += _child_ddl(et.name, f)
    for ix in schema.indexes:
        if ix.applies_to(data_model):
            steps.append(_index_ddl(ix))
    return steps


# --- rows -------------------------------------------------------------------------------------


def _row(key_col: str, key: str, fields, values: dict, extra: "dict | None" = None, every_column: bool = False) -> dict:
    """{column: SQL value} of one row: the key, the extra columns, every present field
    (an explicit null as NULL), every field when `every_column`; a LIST as its text."""
    row = {key_col: key}
    row.update(extra or {})
    for f in fields:
        if f.name in values or every_column:
            v = values.get(f.name)
            row[f.name] = list_text(v) if f.type is FieldType.LIST else v
    return row


def _elements(table: str, owner: str, fields, values: dict) -> list:
    """(child table, row) for every element of every present list."""
    out = []
    for f in fields:
        if f.type is FieldType.LIST and isinstance(values.get(f.name), list):
            for pos, v in enumerate(values[f.name]):
                out.append((child_table(table, f.name), {"owner": owner, "pos": pos, "v": v}))
    return out


_CAST = {FieldType.INT: "BIGINT", FieldType.FLOAT: "DOUBLE", FieldType.STRING: "NVARCHAR(5000)", FieldType.BOOL: "BOOLEAN", FieldType.LIST: "NVARCHAR(5000)"}
_KEY_COLUMNS = ("id", "eid", "src_id", "dst_id", "owner")


def _types_of(instance: Instance):
    """table -> {column: the SQL type a CAST names} (what `INSERT ... SELECT` and
    `MERGE INTO ... USING` type their parameters as)."""
    schema = instance.schema
    declared = {nt.name: schema.query_fields(nt.name) for nt in schema.node_types}
    declared.update({et.name: et.fields for et in schema.edge_types})

    def types(table: str) -> dict:
        out = {c: ID_TYPE for c in _KEY_COLUMNS}
        if table in declared:
            out.update({f.name: _CAST[f.type] for f in declared[table]})
        else:
            out.update({"pos": "INTEGER", "v": "BIGINT"})
        return out

    return types


def _insert(table: str, row: dict, types: dict, via: str) -> Sql:
    cols = list(row)
    params = {f"v{i}": row[c] for i, c in enumerate(cols)}
    if via == "select":
        exprs = ", ".join(f"CAST(:v{i} AS {types[c]})" for i, c in enumerate(cols))
        return Sql(f"INSERT INTO {table} ({', '.join(cols)}) SELECT {exprs} FROM DUMMY", params)
    return Sql(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(f':v{i}' for i in range(len(cols)))})", params)


def _inserts(rows: list, types_of, via: str) -> list:
    """[(table, row)] as steps: one INSERT each, or with `batch` one prepared INSERT per
    run of rows of one table and one column set."""
    if via != "batch":
        return [_insert(t, r, types_of(t), via) for t, r in rows]
    out: list = []
    for t, r in rows:
        cols = tuple(r)
        last = out[-1] if out else None
        text = f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join(f':{c}' for c in cols)})"
        if isinstance(last, SqlBatch) and last.text == text:
            last.rows.append(dict(r))
        else:
            out.append(SqlBatch(text, [dict(r)]))
    return out


def node_rows(instance: Instance, node, values: dict, every_column: bool) -> list:
    fields = instance.schema.query_fields(node.type_name)
    return [(node.type_name, _row("id", node.record_id, fields, values, every_column=every_column))] + _elements(node.type_name, node.record_id, fields, values)


def edge_rows(instance: Instance, edge, values: dict, every_column: bool) -> list:
    et = instance.schema.edge_type(edge.type_name)
    extra = {"src_id": edge.from_record, "dst_id": edge.to_record}
    return [(edge.type_name, _row("eid", edge.record_id, et.fields, values, extra, every_column))] + _elements(edge.type_name, edge.record_id, et.fields, values)


def _replace_elements(table: str, key: str, fields, values: dict, via: str, types_of) -> list:
    steps = [Sql(f"DELETE FROM {child_table(table, f.name)} WHERE owner = :k", {"k": key}) for f in fields if f.type is FieldType.LIST]
    return steps + _inserts(_elements(table, key, fields, values), types_of, via)


def _node_update(instance: Instance, op: NodeUpdate, form, types_of) -> list:
    node = op.node
    t, rid = node.type_name, node.record_id
    fields = instance.schema.query_fields(t)
    final = node.values
    row = _row("id", rid, fields, final, every_column=True)
    cols = [c for c in row if c != "id"]
    params = {f"v{i}": row[c] for i, c in enumerate(cols)}
    params["k"] = rid
    if not cols:
        return []
    if form.update_form == "upsert":
        steps = [Sql(f"UPSERT {t} (id, {', '.join(cols)}) VALUES (:k, {', '.join(f':v{i}' for i in range(len(cols)))}) WHERE id = :k", params)]
    elif form.update_form == "merge":
        types = types_of(t)
        src = ", ".join([f"CAST(:k AS {ID_TYPE}) AS id"] + [f"CAST(:v{i} AS {types[c]}) AS {c}" for i, c in enumerate(cols)])
        sets = ", ".join(f"{t}.{c} = s.{c}" for c in cols)
        steps = [Sql(f"MERGE INTO {t} USING (SELECT {src} FROM DUMMY) s ON {t}.id = s.id WHEN MATCHED THEN UPDATE SET {sets}", params)]
    else:
        sets = ", ".join(f"{c} = :v{i}" for i, c in enumerate(cols))
        steps = [Sql(f"UPDATE {t} SET {sets} WHERE id = :k", params)]
    return steps + _replace_elements(t, rid, fields, final, form.insert_via, types_of)


def _edge_children_delete(et, eid_expr: str, params: dict) -> list:
    return [Sql(f"DELETE FROM {child_table(et.name, f.name)} WHERE owner {eid_expr}", dict(params)) for f in et.fields if f.type is FieldType.LIST]


def _edge_update(instance: Instance, op: EdgeUpdate, form, types_of) -> list:
    e = op.edge
    et = instance.schema.edge_type(e.type_name)
    if form.edge_update_form == "delete_insert":
        steps = _edge_children_delete(et, "= :k", {"k": e.record_id})
        steps.append(Sql(f"DELETE FROM {et.name} WHERE eid = :k", {"k": e.record_id}))
        return steps + _inserts(edge_rows(instance, e, e.values, every_column=False), types_of, form.insert_via)
    row = _row("eid", e.record_id, et.fields, e.values, every_column=True)
    cols = [c for c in row if c != "eid"]
    if not cols:
        return []
    params = {f"v{i}": row[c] for i, c in enumerate(cols)}
    params["k"] = e.record_id
    steps = [Sql(f"UPDATE {et.name} SET {', '.join(f'{c} = :v{i}' for i, c in enumerate(cols))} WHERE eid = :k", params)]
    return steps + _replace_elements(et.name, e.record_id, et.fields, e.values, form.insert_via, types_of)


def _edge_delete(instance: Instance, edge) -> list:
    et = instance.schema.edge_type(edge.type_name)
    return _edge_children_delete(et, "= :k", {"k": edge.record_id}) + [Sql(f"DELETE FROM {et.name} WHERE eid = :k", {"k": edge.record_id})]


def _node_delete(instance: Instance, op: NodeDelete) -> list:
    node = op.node
    steps = []
    for e in op.incident_edges:
        steps += _edge_delete(instance, e)
    fields = instance.schema.query_fields(node.type_name)
    steps += [Sql(f"DELETE FROM {child_table(node.type_name, f.name)} WHERE owner = :k", {"k": node.record_id}) for f in fields if f.type is FieldType.LIST]
    steps.append(Sql(f"DELETE FROM {node.type_name} WHERE id = :k", {"k": node.record_id}))
    return steps


def _junk(instance: Instance, types_of, via: str) -> list:
    """Rows written inside a transaction that is rolled back: none may survive."""
    rows = []
    for nt in instance.schema.node_types:
        fields = instance.schema.query_fields(nt.name)
        for i in range(2):
            values = {f.name: ([i, -i] if f.type is FieldType.LIST else {FieldType.INT: 10**6 + i, FieldType.FLOAT: 0.5 + i, FieldType.STRING: f"junk{i}", FieldType.BOOL: bool(i)}[f.type]) for f in fields}
            rows.append((nt.name, _row("id", f"{nt.name}/junk{i}", fields, values)))
            rows += _elements(nt.name, f"{nt.name}/junk{i}", fields, values)
    for et in instance.schema.edge_types:
        nodes = instance.nodes_of(et.from_type)
        if nodes:
            rows.append((et.name, {"eid": f"{et.name}/junk", "src_id": nodes[0].record_id, "dst_id": nodes[0].record_id}))
    return [Transaction(_inserts(rows, types_of, via), commit=False)] if rows else []


def build_relational_setup(instance: Instance, data_model: str = DATA_MODEL, extra_ddl: "list | None" = None) -> list:
    """The setup plan: the DDL (`ddl`, then `extra_ddl`), the data through the data
    model's write form, the tail."""
    schema = instance.schema
    form = instance.write_form(data_model)
    types_of = _types_of(instance)
    every = form.insert_via == "batch"
    first = initial_node_values(instance)
    rows: list = []
    for node in sorted(initial_nodes(instance), key=lambda n: (n.type_name, n.local_id)):
        rows += node_rows(instance, node, first[node.record_id], every)
    edge_first = initial_edge_values(instance)
    for e in initial_edges(instance):
        rows += edge_rows(instance, e, edge_first[e.record_id], every)
    data = _inserts(rows, types_of, form.insert_via)
    for op in instance.churn:
        if isinstance(op, NodeUpdate):
            data += _node_update(instance, op, form, types_of)
        elif isinstance(op, EdgeUpdate):
            data += _edge_update(instance, op, form, types_of)
        elif isinstance(op, EdgeDelete):
            data += _edge_delete(instance, op.edge)
        elif isinstance(op, NodeDelete):
            data += _node_delete(instance, op)
    tail = _junk(instance, types_of, form.insert_via) if form.cancelled_junk else []
    if form.rebuild_indexes:
        tail += [Sql(f"MERGE DELTA OF {t}") for t in all_tables(instance)]
    return ddl(instance, data_model) + list(extra_ddl or []) + wrap(data, form) + tail


def all_tables(instance: Instance) -> list:
    """Every table of the data model: the node tables, the junctions, the child tables."""
    schema = instance.schema
    owners = [(nt.name, schema.query_fields(nt.name)) for nt in schema.node_types] + [(et.name, et.fields) for et in schema.edge_types]
    return [t for t, _ in owners] + [child_table(t, f.name) for t, fields in owners for f in fields if f.type is FieldType.LIST]

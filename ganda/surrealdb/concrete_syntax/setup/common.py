"""Shared pieces of the three materializers. A materializer produces a `SetupPlan`: an
ordered list of `Surql` steps (`ganda.surrealdb.engine.plan`), each a script of
statements sent in one request; records are addressed by their record ids, spelled from
the logical ids (`dialect.engine_id`), every value a typed literal (`dialect.surql_literal`).

What the three data models share: node records with the same values (a SCHEMAFULL
class declares every field as `option<type | null>`, a SCHEMALESS one takes them as
written), the `eid` of every relationship (the record key of an edge or junction row,
a field of a doc link entry), the churn discipline (initial values first, then update to
the final ones through the data model's drawn update form; churned edges written then
removed; churned-away nodes removed with the per-data-model cascade), the write forms
and the cancelled-junk transaction.
"""
from __future__ import annotations

import base64
from typing import Any

from ganda.core.data.model import (
    EdgeUpdate,
    FieldType,
    IndexSpec,
    Instance,
    NodeUpdate,
    Schema,
    WriteForm,
)
from ganda.surrealdb.engine.dialect import SURREAL_TYPES, UNIQUE_KINDS, engine_id, surql_literal
from ganda.surrealdb.engine.limits import LIMITS
from ganda.surrealdb.engine.plan import Surql
from ganda.surrealdb.provider.physical_design import is_schemafull
from ganda.surrealdb.rules import assumes, works_around
from ganda.surrealdb.rules.catalog import ACCEPTED, SURQL, TRANSACTION


# --- values ---------------------------------------------------------------------------------------

def field_type(schema: Schema, table: str, name: str, edge: bool = False) -> "FieldType | None":
    try:
        return schema.field(table, name, edge=edge).type
    except KeyError:
        return None


def typed(schema: Schema, table: str, values: dict, edge: bool = False) -> dict:
    """{field: literal text} for a record's values."""
    return {k: surql_literal(v, field_type(schema, table, k, edge)) for k, v in values.items()}


def object_literal(literals: dict) -> str:
    return "{ " + ", ".join(f"{k}: {v}" for k, v in literals.items()) + " }"


def set_clause(literals: dict) -> str:
    return ", ".join(f"{k} = {v}" for k, v in literals.items())


def rid(logical: str) -> str:
    return engine_id(logical)


# --- DDL ------------------------------------------------------------------------------------------

# a SCHEMAFULL field admits an explicit stored null beside its type (an omitted field is
# NONE, which `option<>` admits)
assumes("accepts.define_field.option_or_null", SURQL, ACCEPTED)
# a table a query names must exist: every class is declared, SCHEMALESS or SCHEMAFULL
assumes("table.undeclared.select_refused", SURQL, True)


def field_kind(f) -> str:
    kind = SURREAL_TYPES[f.type]
    flexible = " FLEXIBLE" if f.type is FieldType.MAP else ""
    return f"TYPE option<{kind} | null>{flexible}"


def table_statements(name: str, fields: tuple, schemafull: bool, extra: "list | None" = None, relation: "tuple | None" = None) -> list:
    """`DEFINE TABLE` and, SCHEMAFULL, one `DEFINE FIELD` per field; `extra` adds
    (name, kind text) declarations (a doc link list, a junction's endpoints);
    `relation` = (in table, out table) declares a graph edge table."""
    kind = f" TYPE RELATION IN {relation[0]} OUT {relation[1]}" if relation else ""
    if not schemafull:
        return [f"DEFINE TABLE {name}{kind} SCHEMALESS;"]
    out = [f"DEFINE TABLE {name}{kind} SCHEMAFULL;"]
    out += [f"DEFINE FIELD {f.name} ON {name} {field_kind(f)};" for f in fields]
    out += [f"DEFINE FIELD {fname} ON {name} {ftext};" for fname, ftext in extra or []]
    return out


def index_statement(ix: IndexSpec, table: "str | None" = None) -> str:
    unique = " UNIQUE" if ix.kind in UNIQUE_KINDS else ""
    return f"DEFINE INDEX {ix.name()} ON TABLE {table or ix.table} FIELDS {', '.join(ix.all_fields)}{unique};"


def node_ddl(schema: Schema, data_model: str, extra: "callable | None" = None) -> list:
    """Every node class (the base class's fields live on each subclass: the engine has
    no inheritance, a question on the base class reads every subclass table) and the
    node indexes of the data model."""
    out: list = []
    for nt in schema.node_types:
        out += table_statements(nt.name, schema.query_fields(nt.name), is_schemafull(nt, data_model), extra(nt) if extra else None)
    for ix in schema.indexes:
        if ix.target == "node" and ix.applies_to(data_model):
            out.append(index_statement(ix))
    return out


def edge_index_statements(schema: Schema, data_model: str) -> list:
    return [index_statement(ix) for ix in schema.indexes if ix.target == "edge" and ix.applies_to(data_model)]


def edge_schemafull(schema: Schema, et, data_model: str) -> bool:
    """A relationship table follows its source class's schema mode."""
    return is_schemafull(schema.node_type(et.from_type), data_model)


# --- data -----------------------------------------------------------------------------------------

def insert_statement(schema: Schema, node, values: dict, form: WriteForm) -> str:
    """One record through the form's insert door."""
    literals = typed(schema, node.type_name, values)
    r = rid(node.record_id)
    if form.insert_via == "content":
        return f"CREATE {r} CONTENT {object_literal(literals)};"
    if form.insert_via == "insert":
        return f"INSERT INTO {node.type_name} {object_literal({'id': r, **literals})};"
    return f"CREATE {r}" + (f" SET {set_clause(literals)};" if literals else ";")


def patch_literal(final: dict, dropped: list) -> str:
    ops = [f"{{ op: 'remove', path: '/{k}' }}" for k in dropped]
    ops += [f"{{ op: 'replace', path: '/{k}', value: {v} }}" for k, v in final.items()]
    return "[" + ", ".join(ops) + "]"


def update_statements(target: str, literals: dict, dropped: list, form: str, content_extra: "dict | None" = None) -> list:
    """A record from its initial values to its final ones through an update form:
    SET / MERGE (plus `UNSET` of the dropped fields), PATCH (remove and replace ops),
    CONTENT (the whole record, `content_extra` -- what the data model stores beside the
    values, a doc link list -- included so the write does not wipe it)."""
    if form == "content":
        return [f"UPDATE {target} CONTENT {object_literal({**literals, **(content_extra or {})})};"]
    if form == "patch":
        return [f"UPDATE {target} PATCH {patch_literal(literals, dropped)};"] if literals or dropped else []
    out = []
    if dropped:
        out.append(f"UPDATE {target} UNSET {', '.join(dropped)};")
    if literals:
        out.append(f"UPDATE {target} MERGE {object_literal(literals)};" if form == "merge" else f"UPDATE {target} SET {set_clause(literals)};")
    return out


def node_update_statements(instance: Instance, form: WriteForm, extras: "dict | None" = None) -> list:
    """NodeUpdate churn through the data model's drawn update form. `extras` (record
    id -> {field: literal} the data model stores on the record besides its values, i.e.
    the doc data model's link lists as they stand) travels in a CONTENT write."""
    schema = instance.schema
    out: list = []
    for op in instance.churn:
        if not isinstance(op, NodeUpdate):
            continue
        n = op.node
        final = typed(schema, n.type_name, n.values)
        dropped = [k for k in op.initial_values if k not in n.values]
        out += update_statements(rid(n.record_id), final, dropped, form.update_form, (extras or {}).get(n.record_id))
    return out


def edge_update_statements(instance: Instance, form: WriteForm, endpoints: "callable | None" = None) -> list:
    """EdgeUpdate churn on the graph edge table or the junction table: the attributes
    go from their initial values to the final ones through the edge update form (a
    CONTENT write carries `endpoints(edge)` -- the junction's `src` / `dst`)."""
    schema = instance.schema
    out: list = []
    for op in instance.churn:
        if not isinstance(op, EdgeUpdate):
            continue
        e = op.edge
        final = typed(schema, e.type_name, e.values, edge=True)
        dropped = [k for k in op.initial_values if k not in e.values]
        out += update_statements(rid(e.record_id), final, dropped, form.edge_update_form, endpoints(e) if endpoints else None)
    return out


JUNK_MARKER = "__junk"


def _free_value(ftype: FieldType, values: list):
    """A value of the field's own type no row holds -- what the junk record puts in a
    unique-indexed field. None when the type has no free value."""
    if ftype is FieldType.STRING:
        return JUNK_MARKER
    if ftype is FieldType.BINARY:
        return base64.b64encode(JUNK_MARKER.encode()).decode("ascii")
    present = [v for v in values if v is not None]
    if ftype in (FieldType.INT, FieldType.INT32):
        candidates = [424242, -424242, 2**30 + 1, -(2**30) - 1]
    elif ftype in (FieldType.FLOAT, FieldType.FLOAT32, FieldType.DECIMAL):
        candidates = [4242.5, -4242.5, 42.125, -42.125]
    elif ftype in (FieldType.DATETIME, FieldType.DATE):
        candidates = ["2222-02-22 00:00:00", "2333-03-03 00:00:00", "1911-11-11 00:00:00"]
    elif ftype is FieldType.BOOL:
        candidates = [True, False]
    elif ftype is FieldType.MAP:
        return {"kjunk": 424242}
    else:
        return [424242]
    for cand in candidates:
        if not any(type(cand) is type(v) and cand == v for v in present):
            return cand
    return None


# every statement of a cancelled transaction reports itself cancelled and leaves no trace
assumes("transaction.cancelled.statements_report_cancelled", TRANSACTION, True)


def cancelled_junk_steps(instance: Instance, data_model: str) -> list:
    """A `BEGIN; ...; CANCEL;` transaction that creates a junk record, re-saves and
    deletes a real one. Nothing in it may survive (the `cancel_leaks` mutant is the
    leak). The junk record fills every uniquely indexed field with a free value so the
    create is not rejected before the cancel."""
    if not instance.nodes:
        return []
    victim = instance.nodes[0]
    t = victim.type_name
    schema = instance.schema
    junk: "dict | None" = {}
    for ix in schema.indexes:
        if ix.target == "node" and ix.table == t and ix.kind in UNIQUE_KINDS and ix.applies_to(data_model):
            for f in ix.all_fields:
                ftype = field_type(schema, t, f)
                v = _free_value(ftype, [n.values.get(f) for n in instance.nodes_of(t)])
                if v is None:
                    junk = None
                    break
                junk[f] = surql_literal(v, ftype)
            if junk is None:
                break
    parts = ["BEGIN TRANSACTION;"]
    if junk is not None:
        parts.append(f"CREATE {t}:{JUNK_MARKER}" + (f" SET {set_clause(junk)};" if junk else ";"))
    parts.append(f"UPDATE {rid(victim.record_id)};")
    parts.append(f"DELETE {rid(victim.record_id)};")
    parts.append("CANCEL TRANSACTION;")
    return [Surql(" ".join(parts))]


# --- requests -------------------------------------------------------------------------------------

# the body one request may carry: half the server's bound, so a chunk's statements never near it
_REQUEST_BUDGET = LIMITS.max_request_bytes // 2


def _chunks(statements: list, size: int):
    """The statements in pieces of at most `size` (every statement alone when 0) whose
    text stays under the request budget; a statement too big on its own travels alone."""
    if not size:
        for s in statements:
            yield [s]
        return
    chunk: list = []
    used = 0
    for s in statements:
        cost = len(s.encode("utf-8"))
        if chunk and (len(chunk) >= size or used + cost > _REQUEST_BUDGET):
            yield chunk
            chunk, used = [], 0
        chunk.append(s)
        used += cost
    if chunk:
        yield chunk


# how many statements the plain door folds into one request when no chunk size is drawn
# (every statement still commits on its own; probed: one request per statement costs
# ~20 ms of transport each on a laptop)
PLAIN_DOOR_FOLD = 100


def wrap_data(statements: list, form: WriteForm) -> list:
    """The data statements as requests through the form's transaction door: the plain
    door sends them in requests of `PLAIN_DOOR_FOLD` statements, each auto-committed
    on its own; the `tx` door wraps the whole data in one `BEGIN TRANSACTION; ...;
    COMMIT TRANSACTION;`. With `form.chunk_size` (the `statements_per_request` axis)
    either door sends that many per request, a transaction per request."""
    if not statements:
        return []
    size = form.chunk_size or (len(statements) if form.transaction == "tx" else PLAIN_DOOR_FOLD)
    out: list = []
    for chunk in _chunks(statements, size):
        text = "\n".join(chunk)
        if form.transaction == "tx":
            text = f"BEGIN TRANSACTION;\n{text}\nCOMMIT TRANSACTION;"
        out.append(Surql(text))
    return out


# an index defined inside a transaction after its data stays empty (F21), and a REMOVE
# INDEX inside one fails the commit (F22): index DDL travels outside the data's door
works_around("F21")
works_around("F22")


def finalize(ddl: list, data: list, tail: list, instance: Instance, data_model: str, form: WriteForm, indexes: "list | None" = None) -> list:
    """The plan: the DDL in one request, the data through the door, the tail, the junk
    transaction, and a rebuild of every index of the data model when drawn."""
    plan = [Surql("\n".join(ddl))] if ddl else []
    plan += wrap_data(data, form)
    plan += [Surql("\n".join(tail))] if tail else []
    if form.cancelled_junk:
        plan += cancelled_junk_steps(instance, data_model)
    if form.rebuild_indexes and indexes:
        plan.append(Surql("\n".join(f"REBUILD INDEX {ix.name()} ON TABLE {table};" for ix, table in indexes)))
    return plan


def data_model_indexes(schema: Schema, data_model: str, edge_table=None) -> list:
    """[(IndexSpec, table)] the data model defines: the node indexes and, through
    `edge_table(et)`, the edge indexes."""
    out = [(ix, ix.table) for ix in schema.indexes if ix.target == "node" and ix.applies_to(data_model)]
    if edge_table is not None:
        out += [(ix, edge_table(ix.table)) for ix in schema.indexes if ix.target == "edge" and ix.applies_to(data_model)]
    return out

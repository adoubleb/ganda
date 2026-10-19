"""The RDF data model: the Knowledge Graph Engine's triple store, one named graph per
database (the texts name it `dialect.GRAPH_TOKEN`; the session rebinds it).

    <r:n1/r0> a c:n1 ; p:id "n1/r0" ; p:f1 5 ; p:f3 "[1,2]" ; i:f3 1, 2 .
    <r:e1/e0> a c:e1 ; p:eid "e1/e0" ; p:src <r:n1/r0> ; p:dst <r:n2/r3> ; p:w 3 .
    <r:n1/r0> l:e1 <r:n2/r3> .

A node is an IRI with its class (and, under the inheritance axis, the base class too:
`a c:n1, c:N0`, `class_triples`), its logical id and a literal per present field (a
null field has no triple); a LIST is its canonical JSON text plus one `i:` triple per
distinct element (membership is all the elements are asked for). RDF holds a SET of
triples, so a relationship is a resource of its own -- two parallel edges are two
resources -- and the DIRECT link `src l:e dst` the property paths walk is held once per
endpoint pair: the mapping writes it with the first relationship of the pair and
removes it with the last one.

The setup inserts the initial records (`INSERT DATA`, or `INSERT ... WHERE {}`, in
requests of `WriteForm.chunk_size` records or one per record), then replays the churn:
a NodeUpdate deletes the node's field triples and inserts the final ones (by pattern,
by the exact old triples, or in one `DELETE ... INSERT ... WHERE`), an EdgeUpdate does
the same on the relationship resource, an EdgeDelete deletes the resource and, with
the last relationship of its pair, the link, a NodeDelete its relationships and then
every triple of the node. The transaction door wraps the updates in one SQL
transaction. There is no cancelled-junk block on this data model: whether a SPARQL
update joins the SQL transaction is a fact to probe first (`RULES.md`).
"""
from __future__ import annotations

from collections import Counter

from ganda.core.data.model import EdgeDelete, EdgeUpdate, FieldType, Instance, NodeDelete, NodeUpdate
from ganda.hana.dialect import GRAPH_TOKEN, PREFIX_TEXT, record_iri, sparql_literal, sparql_string
from ganda.hana.mapping.common import Declare, Sparql, chunks, initial_edge_values, initial_edges, initial_node_values, initial_nodes, wrap

DATA_MODEL = "rdf"
DEFAULT_CHUNK = 100


def _value_predicates(fields) -> str:
    """`VALUES ?p { ... }`: the predicates a record's values live under (its class, id,
    endpoints and direct links are not among them)."""
    preds = [f"p:{f.name}" for f in fields] + [f"i:{f.name}" for f in fields if f.type is FieldType.LIST]
    return f"VALUES ?p {{ {' '.join(preds)} }}"


def value_triples(subject: str, fields, values: dict) -> list:
    out = []
    for f in fields:
        v = values.get(f.name)
        if v is None:
            continue
        out.append(f"{subject} p:{f.name} {sparql_literal(v)}")
        if f.type is FieldType.LIST:
            for x in sorted(set(v)):
                out.append(f"{subject} i:{f.name} {sparql_literal(x)}")
    return out


def class_triples(subject: str, type_name: str, schema) -> list:
    """`<s> a c:n1`, and under the inheritance axis `<s> a c:N0` as well: a node is a
    member of its class and of the base class, so a query on either names its class."""
    out = [f"{subject} a c:{type_name}"]
    if schema.base is not None and not schema.is_base(type_name) and any(nt.name == type_name for nt in schema.node_types):
        out.append(f"{subject} a c:{schema.base.name}")
    return out


def node_triples(instance: Instance, node, values: dict) -> list:
    s = record_iri(node.record_id)
    head = class_triples(s, node.type_name, instance.schema) + [f"{s} p:id {sparql_string(node.record_id)}"]
    return head + value_triples(s, instance.schema.query_fields(node.type_name), values)


def edge_triples(instance: Instance, edge, values: dict) -> list:
    e = record_iri(edge.record_id)
    head = [f"{e} a c:{edge.type_name}", f"{e} p:eid {sparql_string(edge.record_id)}", f"{e} p:src {record_iri(edge.from_record)}", f"{e} p:dst {record_iri(edge.to_record)}"]
    return head + value_triples(e, instance.schema.edge_type(edge.type_name).fields, values)


def link_triple(edge) -> str:
    return f"{record_iri(edge.from_record)} l:{edge.type_name} {record_iri(edge.to_record)}"


def insert_data(triples: list, via: str = "values") -> Sparql:
    body = " . ".join(triples)
    if via == "select":
        return Sparql(f"{PREFIX_TEXT} INSERT {{ GRAPH {GRAPH_TOKEN} {{ {body} }} }} WHERE {{}}")
    return Sparql(f"{PREFIX_TEXT} INSERT DATA {{ GRAPH {GRAPH_TOKEN} {{ {body} }} }}")


def delete_data(triples: list) -> Sparql:
    return Sparql(f"{PREFIX_TEXT} DELETE DATA {{ GRAPH {GRAPH_TOKEN} {{ {' . '.join(triples)} }} }}")


def delete_values(subject: str, fields) -> Sparql:
    """Every value triple of a record, by pattern."""
    return Sparql(f"{PREFIX_TEXT} DELETE {{ GRAPH {GRAPH_TOKEN} {{ {subject} ?p ?o }} }} WHERE {{ {_value_predicates(fields)} GRAPH {GRAPH_TOKEN} {{ {subject} ?p ?o }} }}")


def replace_values(subject: str, fields, new: list) -> Sparql:
    """One request: the record's value triples deleted and the new ones inserted (the
    WHERE matches once even when the record has none, through the OPTIONAL)."""
    ins = " . ".join(new)
    return Sparql(f"{PREFIX_TEXT} DELETE {{ GRAPH {GRAPH_TOKEN} {{ {subject} ?p ?o }} }} INSERT {{ GRAPH {GRAPH_TOKEN} {{ {ins} }} }} WHERE {{ OPTIONAL {{ {_value_predicates(fields)} GRAPH {GRAPH_TOKEN} {{ {subject} ?p ?o }} }} }}")


def delete_all(subject: str) -> Sparql:
    return Sparql(f"{PREFIX_TEXT} DELETE WHERE {{ GRAPH {GRAPH_TOKEN} {{ {subject} ?p ?o }} }}")


def _update(subject: str, fields, old: dict, new: dict, form: str) -> list:
    new_triples = value_triples(subject, fields, new)
    if form == "merge":
        return [replace_values(subject, fields, new_triples)] if new_triples else [delete_values(subject, fields)]
    if form == "upsert":
        old_triples = value_triples(subject, fields, old)
        steps = [delete_data(old_triples)] if old_triples else []
    else:
        steps = [delete_values(subject, fields)]
    return steps + ([insert_data(new_triples)] if new_triples else [])


class _Links:
    """The direct links' multiplicity: the relationships of each endpoint pair."""

    def __init__(self):
        self.count = Counter()

    def add(self, edge) -> bool:
        k = (edge.type_name, edge.from_record, edge.to_record)
        self.count[k] += 1
        return self.count[k] == 1

    def remove(self, edge) -> bool:
        k = (edge.type_name, edge.from_record, edge.to_record)
        self.count[k] -= 1
        return self.count[k] == 0


def _edge_delete(edge, links: _Links) -> list:
    steps = [delete_all(record_iri(edge.record_id))]
    if links.remove(edge):
        steps.append(delete_data([link_triple(edge)]))
    return steps


def build_rdf_setup(instance: Instance) -> list:
    form = instance.write_form(DATA_MODEL)
    size = 1 if form.insert_via == "batch" else (form.chunk_size or DEFAULT_CHUNK)
    first = initial_node_values(instance)
    records = [node_triples(instance, n, first[n.record_id]) for n in sorted(initial_nodes(instance), key=lambda n: (n.type_name, n.local_id))]
    links = _Links()
    edge_first = initial_edge_values(instance)
    for e in initial_edges(instance):
        triples = edge_triples(instance, e, edge_first[e.record_id])
        if links.add(e):
            triples.append(link_triple(e))
        records.append(triples)
    data = [insert_data([t for r in chunk for t in r], form.insert_via) for chunk in chunks(records, size)]
    schema = instance.schema
    for op in instance.churn:
        if isinstance(op, NodeUpdate):
            n = op.node
            data += _update(record_iri(n.record_id), schema.query_fields(n.type_name), op.initial_values, n.values, form.update_form)
        elif isinstance(op, EdgeUpdate):
            e = op.edge
            if form.edge_update_form == "delete_insert":
                data += [delete_all(record_iri(e.record_id)), insert_data(edge_triples(instance, e, e.values))]
            else:
                data += _update(record_iri(e.record_id), schema.edge_type(e.type_name).fields, op.initial_values, e.values, "set")
        elif isinstance(op, EdgeDelete):
            data += _edge_delete(op.edge, links)
        elif isinstance(op, NodeDelete):
            for e in op.incident_edges:
                data += _edge_delete(e, links)
            data.append(delete_all(record_iri(op.node.record_id)))
    from ganda.hana.mapping.relational import declared_types

    return [Declare(declared_types(instance))] + wrap(data, form)

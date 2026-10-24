"""The Instance itself, read the way `state_check.compare_states` reads a data model.

A unit test of a mapper or a stand-in puts it next to the replayed data models: a
plan that does not replay to the Instance is then off the consensus. The fuzzer never
does this -- its state check compares the data models with each other only.
"""
from __future__ import annotations


class InstanceSession:
    def __init__(self, instance):
        self.instance = instance

    def read_nodes(self, table, ids=None):
        rows = {n.record_id: dict(n.values) for n in self.instance.nodes_of(table)}
        return rows if ids is None else {k: v for k, v in rows.items() if k in set(ids)}

    def read_edges(self, data_model, instance, et, sources=None):
        return [(e.record_id, e.from_record, e.to_record, {k: v for k, v in e.values.items() if v is not None}) for e in self.instance.edges_of(et.name) if sources is None or e.from_record in set(sources)]

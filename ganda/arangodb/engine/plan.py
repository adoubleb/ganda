"""ArangoDB's setup-plan steps, run by `connection.ArangoSession.apply_setup`; each op
name is prefixed `arango_`."""
from __future__ import annotations

from dataclasses import dataclass, field

from ganda.core.engine.setup_plan import step, step_from_dict


@step("arango_collection")
@dataclass
class CreateCollection:
    name: str
    edge: bool = False

    def to_dict(self) -> dict:
        return {"op": "arango_collection", "name": self.name, "edge": self.edge}

    @classmethod
    def from_dict(cls, d: dict) -> "CreateCollection":
        return cls(d["name"], d.get("edge", False))


@step("arango_index")
@dataclass
class CreateIndex:
    collection: str
    fields: list
    name: str
    unique: bool = False
    sparse: bool = False

    def to_dict(self) -> dict:
        return {"op": "arango_index", "collection": self.collection, "fields": list(self.fields), "name": self.name, "unique": self.unique, "sparse": self.sparse}

    @classmethod
    def from_dict(cls, d: dict) -> "CreateIndex":
        return cls(d["collection"], list(d["fields"]), d["name"], d.get("unique", False), d.get("sparse", False))


@step("arango_insert")
@dataclass
class Insert:
    collection: str
    documents: list
    via: str = "api"

    def to_dict(self) -> dict:
        return {"op": "arango_insert", "collection": self.collection, "via": self.via, "documents": self.documents}

    @classmethod
    def from_dict(cls, d: dict) -> "Insert":
        return cls(d["collection"], d["documents"], d.get("via", "api"))


@step("arango_aql")
@dataclass
class Aql:
    query: str
    bind_vars: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"op": "arango_aql", "query": self.query, "bind_vars": self.bind_vars}

    @classmethod
    def from_dict(cls, d: dict) -> "Aql":
        return cls(d["query"], d.get("bind_vars") or {})


@step("arango_document")
@dataclass
class DocumentApi:
    method: str
    collection: str
    key: str
    body: "dict | None" = None
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"op": "arango_document", "method": self.method, "collection": self.collection, "key": self.key, "body": self.body, "params": dict(self.params)}

    @classmethod
    def from_dict(cls, d: dict) -> "DocumentApi":
        return cls(d["method"], d["collection"], d["key"], d.get("body"), d.get("params") or {})


@step("arango_transaction")
@dataclass
class Transaction:
    steps: list
    collections: list
    commit: bool = True

    def to_dict(self) -> dict:
        return {"op": "arango_transaction", "commit": self.commit, "collections": list(self.collections), "steps": [s.to_dict() for s in self.steps]}

    @classmethod
    def from_dict(cls, d: dict) -> "Transaction":
        return cls([step_from_dict(s) for s in d["steps"]], list(d.get("collections", [])), d.get("commit", True))

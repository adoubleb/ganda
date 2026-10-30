"""ArangoDB's setup-plan vocabulary: the steps `connection.ArangoSession.apply_setup`
executes.

ArangoDB has no multi-statement script language: schema lives behind REST endpoints
(`/_api/collection`, `/_api/index`) and data goes through the document API, AQL or a
stream transaction. A step is a plain dataclass with a JSON `to_dict()`, so a bug
bundle records the exact plan that ran (`ganda.core.engine.setup_plan`); its op name is
prefixed `arango_` (the step registry is shared by every provider):

    CreateCollection(name, edge)       `POST /_api/collection`
    CreateIndex(collection, fields, name, unique, sparse)   a persistent index
    Insert(collection, documents, via) a bulk insert: the document API (`api`), or
                                       `FOR d IN @docs INSERT d INTO c` (`aql`)
    Aql(query, bind_vars)              one AQL statement
    DocumentApi(method, collection, key, body, params)   PATCH / PUT / DELETE one document
    Transaction(steps, collections, commit)   the steps in one stream transaction

The mappers that write plans are `concrete_syntax/setup/`.
"""
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

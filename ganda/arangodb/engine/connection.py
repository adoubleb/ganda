"""ArangoDB's connection layer: a server in Docker and one HTTP session per database.

AQL runs through the cursor API with every value a bind variable (`@p`), which is
exactly the case's `bind_vars` -- narrowed per text to the names it uses, because a
bind variable a query does not name is errorNum 1552 (and one it names and lacks is the
engine's own error, 1551). A variant's options (`QueryCase.variant_options`) travel as
the cursor's `options`; `failOnWarning` turns a warning into an error on every data
model alike; `maxRuntime` is the session's query timeout. Every data model of a seed is
its own database.

Three error classes:
- `EngineError`: the server understood the request and rejected it (a parse error, a
  unique-constraint violation). `internal` marks a 5xx the server ANSWERED (an error
  from inside the engine: a finding on its own); `timeout` marks a query the engine
  killed at `maxRuntime` (errorNum 1500) or one the transport gave up on while the
  server still answers (no answer, not a rejection: the oracle leaves it out).
- `EngineCrash`: the server could not be talked to at all.

The transaction door (`ganda.arangodb.engine.tx`): a door text begins a stream transaction
over the collections it names, runs its statements and its question inside it, and
aborts it whatever happened. A setup plan's `Transaction` step is the same stream
transaction, committed.
"""
from __future__ import annotations

import os
import re
from urllib.parse import quote

import requests

from ganda.core.engine.docker import DockerServer, cleanup_orphans  # noqa: F401
from ganda.core.engine.provider import EngineCrash, EngineError, Session
from ganda.arangodb.engine import tx
from ganda.arangodb.storage.dialect import engine_id, logical_id
from ganda.arangodb.engine.limits import STREAMING_TRANSACTION_MAX_BYTES
from ganda.arangodb.storage.mapping.common import Aql, CreateCollection, CreateIndex, DocumentApi, Insert, Transaction

DEFAULT_PASSWORD = "fuzz"
DEFAULT_TIMEOUT_MS = 30000
QUERY_OPTIONS = {"failOnWarning": True}
_VOLATILE_KEYS = ("_rev",)
# no answer: a query the engine killed (`maxRuntime`, 1500), or a cursor gone before its
# next batch was fetched (1600: the query killed meanwhile, or the cursor's time to live
# over -- a self-join over thousands of records answers millions of ids)
_TIMEOUT_NUMS = (1500, 1600)
# how long a cursor waits between two batch fetches
CURSOR_TTL_S = 3600
_TIMEOUT_RE = re.compile(r"query killed|timed? ?out", re.I)
# a 5xx the engine documents as a rejection: a forced index hint the index cannot serve
_EXPECTED_5XX = (1577,)
# how many ids one `IN @ids` lookup carries
_IN_CHUNK = 500
_BIND_RE = re.compile(r"@(@?[A-Za-z_][A-Za-z0-9_]*)")


def bind_subset(text: str, binds: dict) -> dict:
    """The bind variables the text names (a superfluous one is errorNum 1552)."""
    names = set(_BIND_RE.findall(text))
    return {k: v for k, v in binds.items() if k in names}


def _strip(row):
    if isinstance(row, dict):
        return {k: _strip(v) for k, v in row.items() if k not in _VOLATILE_KEYS}
    if isinstance(row, list):
        return [_strip(v) for v in row]
    return row


def _attrs(et, row: dict) -> dict:
    return {f.name: row.get(f.name) for f in et.fields if f.name in row and row.get(f.name) is not None}


class ArangoSession(Session):
    def __init__(self, base_url: str, password: str, database: str, user: str = "root", timeout: float = 120.0, query_timeout_ms: int = DEFAULT_TIMEOUT_MS):
        self.base_url = base_url.rstrip("/")
        self.database = database
        self.timeout = timeout
        self.query_timeout_ms = query_timeout_ms
        self._http = requests.Session()
        self._http.auth = (user, password)

    # --- transport ---------------------------------------------------------------------

    def _request(self, method: str, path: str, body=None, params=None, headers=None, system: bool = False):
        url = f"{self.base_url}{'' if system else '/_db/' + self.database}{path}"
        try:
            resp = self._http.request(method, url, json=body, params=params, headers=headers, timeout=self.timeout)
        except requests.exceptions.Timeout as e:
            if self._alive():
                raise EngineError(f"{method} {path} gave no answer within {self.timeout:.0f}s (the server still answers)", timeout=True) from e
            raise EngineCrash(f"could not reach ArangoDB at {self.base_url}: {e}") from e
        except requests.exceptions.RequestException as e:
            raise EngineCrash(f"could not reach ArangoDB at {self.base_url}: {e}") from e
        if resp.status_code == 204:
            return None
        try:
            payload = resp.json()
        except ValueError as e:
            if resp.status_code >= 500:
                raise EngineCrash(f"ArangoDB {resp.status_code} non-JSON on {method} {path}: {resp.text[:300]}") from e
            raise EngineError(f"{method} {path} answered {resp.status_code}: {resp.text[:500]}", status=resp.status_code)
        if resp.status_code >= 400 or (isinstance(payload, dict) and payload.get("error")):
            message = payload.get("errorMessage", resp.text[:500]) if isinstance(payload, dict) else resp.text[:500]
            num = payload.get("errorNum") if isinstance(payload, dict) else None
            timeout = num in _TIMEOUT_NUMS or bool(_TIMEOUT_RE.search(str(message)))
            # a 5xx the server ANSWERED is an internal error, not a dead server
            raise EngineError(f"{method} {path} rejected ({resp.status_code}, errorNum={num}): {message}", internal=resp.status_code >= 500 and resp.status_code != 503 and num not in _EXPECTED_5XX, status=resp.status_code, timeout=timeout)
        return payload

    def _alive(self) -> bool:
        try:
            return requests.get(f"{self.base_url}/_api/version", auth=self._http.auth, timeout=5).status_code == 200
        except requests.exceptions.RequestException:
            return False

    # --- queries -----------------------------------------------------------------------

    def aql(self, query: str, binds: "dict | None" = None, trx: "str | None" = None, options: "dict | None" = None, timeout_ms: "int | None" = None) -> list:
        """One AQL query with the bind variables it names; the whole result (the cursor
        followed), `_rev` stripped. `timeout_ms` is the query's `maxRuntime` (the
        session's unless given; 0 for none)."""
        headers = {"x-arango-trx-id": trx} if trx else None
        opts = {**QUERY_OPTIONS, **(options or {})}
        ms = self.query_timeout_ms if timeout_ms is None else timeout_ms
        if ms:
            opts["maxRuntime"] = ms / 1000.0
        body = {"query": query, "bindVars": bind_subset(query, binds or {}), "batchSize": 5000, "ttl": CURSOR_TTL_S, "options": opts}
        payload = self._request("POST", "/_api/cursor", body, headers=headers)
        rows = list(payload.get("result", []))
        while payload.get("hasMore"):
            payload = self._request("PUT", f"/_api/cursor/{payload['id']}", headers=headers)
            rows.extend(payload.get("result", []))
        return [_strip(r) for r in rows]

    def execute(self, text: str, binds: "dict | None" = None, options: "dict | None" = None) -> list:
        """One query text of a case: a transaction door script, or one AQL query."""
        if tx.is_script(text):
            return self._door(text, binds or {}, options)
        return self.aql(text, binds, options=options)

    def _door(self, text: str, binds: dict, options: "dict | None") -> list:
        collections, statements, question = tx.parts(text)
        trx = self.begin_transaction(collections)
        try:
            for s in statements:
                self.aql(s, binds, trx=trx, timeout_ms=0)
            return self.aql(question, binds, trx=trx, options=options)
        finally:
            try:
                self.abort_transaction(trx)
            except (EngineError, EngineCrash):
                pass

    def explain(self, text: str, binds: "dict | None" = None) -> "list | None":
        """The plan's node types with their index names -- a plan fingerprint; None for
        a door script."""
        if tx.is_script(text):
            return None
        payload = self._request("POST", "/_api/explain", {"query": text, "bindVars": bind_subset(text, binds or {})})
        return [node.get("type", "?") + (":" + ",".join(ix if isinstance(ix, str) else ix.get("name", "") for ix in node["indexes"]) if node.get("indexes") else "") for node in payload.get("plan", {}).get("nodes", [])]

    def begin_transaction(self, collections) -> str:
        """`collections`: a list of write collections, or `{"write": [...], "read": [...]}`."""
        spec = {"write": list(collections)} if isinstance(collections, (list, tuple)) else dict(collections)
        return self._request("POST", "/_api/transaction/begin", {"collections": spec})["result"]["id"]

    def commit_transaction(self, trx: str) -> None:
        self._request("PUT", f"/_api/transaction/{trx}")

    def abort_transaction(self, trx: str) -> None:
        self._request("DELETE", f"/_api/transaction/{trx}")

    # --- setup plans -------------------------------------------------------------------

    def apply_setup(self, plan: list) -> None:
        for step in plan:
            self.apply_step(step, None)

    def apply_step(self, step, trx: "str | None") -> None:
        headers = {"x-arango-trx-id": trx} if trx else None
        if isinstance(step, CreateCollection):
            self._request("POST", "/_api/collection", {"name": step.name, "type": 3 if step.edge else 2})
        elif isinstance(step, CreateIndex):
            self._request("POST", "/_api/index", {"type": "persistent", "fields": list(step.fields), "name": step.name, "unique": step.unique, "sparse": step.sparse}, params={"collection": step.collection})
        elif isinstance(step, Insert):
            if not step.documents:
                return
            if step.via == "aql":
                self.aql(f"FOR d IN @docs INSERT d INTO {step.collection}", {"docs": step.documents}, trx=trx, timeout_ms=0)
                return
            result = self._request("POST", f"/_api/document/{step.collection}", step.documents, headers=headers)
            # a bulk insert answers 202 even when a document failed: each failed entry
            # carries `error: true`; the first one is the rejection
            if isinstance(result, list):
                for entry in result:
                    if isinstance(entry, dict) and entry.get("error"):
                        raise EngineError(f"insert into {step.collection} rejected (errorNum={entry.get('errorNum')}): {entry.get('errorMessage')}")
        elif isinstance(step, Aql):
            self.aql(step.query, step.bind_vars, trx=trx, timeout_ms=0)
        elif isinstance(step, DocumentApi):
            if not step.key:
                # the bulk form: an array of documents each naming its `_key`; a failed
                # entry is reported inside the array
                result = self._request(step.method, f"/_api/document/{step.collection}", step.body, params=step.params or None, headers=headers)
                if isinstance(result, list):
                    for entry in result:
                        if isinstance(entry, dict) and entry.get("error"):
                            raise EngineError(f"{step.method} {step.collection} rejected (errorNum={entry.get('errorNum')}): {entry.get('errorMessage')}")
                return
            # the key is percent-encoded in the path: `%`, `;`, `+` and `@` are legal in one
            self._request(step.method, f"/_api/document/{step.collection}/{quote(step.key, safe='')}", step.body, params=step.params or None, headers=headers)
        elif isinstance(step, Transaction):
            inner = self.begin_transaction(step.collections)
            try:
                for s in step.steps:
                    self.apply_step(s, inner)
            except (EngineError, EngineCrash):
                try:
                    self.abort_transaction(inner)
                except (EngineError, EngineCrash):
                    pass
                raise
            if step.commit:
                self.commit_transaction(inner)
            else:
                self.abort_transaction(inner)
        else:
            raise TypeError(f"unknown setup step {step!r}")

    # --- reading the stored state back (`ganda.core.engine.state_check`) ----------------------

    def _by_ids(self, query: str, ids: list) -> list:
        """`query` (naming `@ids`) over `_IN_CHUNK` ids at a time."""
        out: list = []
        for k in range(0, len(ids), _IN_CHUNK):
            out.extend(self.aql(query, {"ids": list(ids[k : k + _IN_CHUNK])}, timeout_ms=0))
        return out

    def read_nodes(self, table: str, ids: "list | None" = None) -> dict:
        if ids is None:
            rows = self.aql(f"FOR n IN {table} RETURN n", timeout_ms=0)
        else:
            rows = self._by_ids(f"FOR n IN {table} FILTER n.id IN @ids RETURN n", list(ids))
        return {r["id"]: r for r in rows if isinstance(r, dict) and "id" in r}

    def count_nodes(self, table: str) -> int:
        return int(self.aql(f"RETURN LENGTH({table})", timeout_ms=0)[0])

    def read_edges(self, data_model: str, instance, et, sources: "list | None" = None) -> list:
        """[(eid, from id, to id, {attrs})] as the data model stores them; with
        `sources`, those leaving the given records only."""
        out = []
        if data_model == "graph":
            if sources is None:
                rows = self.aql(f"FOR e IN {et.name} RETURN e", timeout_ms=0)
            else:
                rows = self._by_ids(f"FOR e IN {et.name} FILTER e._from IN @ids RETURN e", [engine_id(s) for s in sources])
            for r in rows:
                out.append((r.get("eid"), logical_id(r["_from"]) if r.get("_from") else None, logical_id(r["_to"]) if r.get("_to") else None, _attrs(et, r)))
        elif data_model == "rel":
            if sources is None:
                rows = self.aql(f"FOR e IN {et.name} RETURN e", timeout_ms=0)
            else:
                rows = self._by_ids(f"FOR e IN {et.name} FILTER e.src_id IN @ids RETURN e", list(sources))
            for r in rows:
                out.append((r.get("eid"), r.get("src_id"), r.get("dst_id"), _attrs(et, r)))
        else:
            select = f"FOR s IN {et.from_type} %s RETURN {{id: s.id, links: s.{et.name}}}"
            if sources is None:
                rows = self.aql(select % "", timeout_ms=0)
            else:
                rows = self._by_ids(select % "FILTER s.id IN @ids", list(sources))
            for r in rows:
                for link in r.get("links") or []:
                    if not isinstance(link, dict):
                        continue
                    target = link.get("target")
                    out.append((link.get("eid"), r["id"], logical_id(target) if isinstance(target, str) and "/" in target else None, _attrs(et, link)))
        return out

    def count_edges(self, data_model: str, instance, et) -> int:
        if data_model == "doc":
            rows = self.aql(f"RETURN SUM(FOR s IN {et.from_type} RETURN LENGTH(s.{et.name} || []))", timeout_ms=0)
            return int(rows[0] or 0) if rows else 0
        return int(self.aql(f"RETURN LENGTH({et.name})", timeout_ms=0)[0])

    def ping(self):
        return self.aql("RETURN 1")

    def collections(self) -> list:
        payload = self._request("GET", "/_api/collection", params={"excludeSystem": "true"})
        return sorted(c["name"] for c in payload.get("result", []))


class ArangoServer(DockerServer):
    """A running ArangoDB server (`arangodb/arangodb:latest`, or `ARANGO_IMAGE`), or one
    reached at `ARANGO_URL` (with `ARANGO_PASSWORD`). `query_timeout_ms` is every
    session's `maxRuntime`, with the HTTP read timeout a minute past it;
    `streaming_max_bytes` the server's bound on one stream transaction's writes (a
    data model's whole setup may run in one)."""

    engine = "arangodb"
    image = os.environ.get("ARANGO_IMAGE", "arangodb/arangodb:latest")
    container_port = 8529
    url_env = "ARANGO_URL"

    def __init__(self, image: "str | None" = None, memory_limit: "str | None" = None, query_timeout_ms: int = DEFAULT_TIMEOUT_MS, streaming_max_bytes: "int | None" = STREAMING_TRANSACTION_MAX_BYTES):
        super().__init__(image, memory_limit)
        self.query_timeout_ms = query_timeout_ms
        self.streaming_max_bytes = streaming_max_bytes

    @property
    def password(self) -> str:
        return os.environ.get("ARANGO_PASSWORD", DEFAULT_PASSWORD) if self.external else DEFAULT_PASSWORD

    def environment(self) -> dict:
        return {"ARANGO_ROOT_PASSWORD": DEFAULT_PASSWORD}

    def command(self) -> list:
        return [f"--transaction.streaming-max-transaction-size={self.streaming_max_bytes}"] if self.streaming_max_bytes else []

    def is_ready(self) -> bool:
        return requests.get(f"{self.base_url}/_api/version", auth=("root", self.password), timeout=1.0).status_code == 200

    def session(self, database: str) -> ArangoSession:
        return ArangoSession(self.base_url, self.password, database, timeout=max(120.0, self.query_timeout_ms / 1000.0 + 60.0), query_timeout_ms=self.query_timeout_ms)

    def drop_database(self, database: str) -> None:
        try:
            self.session("_system")._request("DELETE", f"/_api/database/{database}")
        except EngineError as e:
            if e.status != 404:
                raise

    def fresh_session(self, database: str) -> ArangoSession:
        self.drop_database(database)
        self.session("_system")._request("POST", "/_api/database", {"name": database})
        return self.session(database)


def db_name(ns: str, data_model: str) -> str:
    return f"fz_{ns}_{data_model}"

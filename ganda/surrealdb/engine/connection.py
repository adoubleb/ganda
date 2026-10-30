"""SurrealDB's connection layer: a server in Docker, one `/sql` session per database (a `USE` preamble, binds as a `LET` prelude).
`EngineError` is a rejected statement (`internal` / `timeout` mark faults and timeouts); `EngineCrash` an unreachable server or a 5xx."""
from __future__ import annotations

import json
import os
import re

import requests

from ganda.core.engine.docker import DockerServer, cleanup_orphans  # noqa: F401
from ganda.core.engine.provider import EngineCrash, EngineError, Session
from ganda.core.rules.model import ErrorKind
from ganda.surrealdb.engine.dialect import engine_id, logical_id, surql_literal
from ganda.surrealdb.engine.limits import HTTP_MAX_SQL_BODY_SIZE
from ganda.surrealdb.engine.plan import Surql
from ganda.surrealdb.rules import CATALOG, assumes
from ganda.surrealdb.rules.catalog import TRANSACTION

NAMESPACE = "ganda"
DEFAULT_IMAGE = "surrealdb/surrealdb:v3.3.4"
DEFAULT_USER = "root"
DEFAULT_PASSWORD = "root"
DEFAULT_TIMEOUT_MS = 30000
CANCELLED = "The query was not executed due to a cancelled transaction"
# how many ids one `id IN [...]` lookup carries
_IN_CHUNK = 500
_SELECT_RE = re.compile(r"^\s*SELECT\b", re.IGNORECASE)

assumes("transaction.cancelled.statements_report_cancelled", TRANSACTION, True)


def db_name(namespace: str, data_model: str) -> str:
    return f"fz_{namespace}_{data_model}"


def _strip(row):
    """A row without the engine's record id: `id` becomes the logical id."""
    if isinstance(row, dict) and "id" in row:
        lid = logical_id(row["id"])
        if lid is not None:
            return {**row, "id": lid}
    return row


class SurrealSession(Session):
    def __init__(self, base_url: str, database: str, user: str = DEFAULT_USER, password: str = DEFAULT_PASSWORD, timeout: float = 120.0, query_timeout_ms: int = DEFAULT_TIMEOUT_MS):
        self.base_url = base_url.rstrip("/")
        self.database = database
        self.timeout = timeout
        self.query_timeout_ms = query_timeout_ms
        self._http = requests.Session()
        self._http.auth = (user, password)
        self._http.headers.update({"Accept": "application/json", "Content-Type": "text/plain"})

    # --- transport ---------------------------------------------------------------------

    def _alive(self) -> bool:
        try:
            return requests.get(f"{self.base_url}/health", timeout=5).status_code == 200
        except requests.exceptions.RequestException:
            return False

    def query(self, surql: str) -> list:
        """Every statement's result, in order (the USE preamble's dropped); `EngineError`
        when one failed, unless the failure is a cancelled transaction's and a later
        statement answered."""
        full = f"USE NS {NAMESPACE} DB {self.database};\n{surql}"
        try:
            resp = self._http.post(f"{self.base_url}/sql", data=full.encode("utf-8"), timeout=self.timeout)
        except requests.exceptions.Timeout as e:
            if self._alive():
                raise EngineError(f"no answer within {self.timeout:.0f}s (the server still answers)", timeout=True) from e
            raise EngineCrash(f"could not reach SurrealDB at {self.base_url}: {e}") from e
        except requests.exceptions.RequestException as e:
            raise EngineCrash(f"could not reach SurrealDB at {self.base_url}: {e}") from e
        if resp.status_code >= 500:
            raise EngineCrash(f"SurrealDB answered {resp.status_code}: {resp.text[:300]}")
        if resp.status_code >= 400:
            raise EngineError(f"request rejected ({resp.status_code}): {resp.text[:800]}", status=resp.status_code)
        try:
            statements = resp.json()
        except ValueError as e:
            raise EngineCrash(f"malformed response from SurrealDB: {e}") from e
        results = []
        for i, s in enumerate(statements):
            if s.get("status") == "OK":
                results.append(s.get("result"))
                continue
            message = str(s.get("result"))
            if message == CANCELLED and i < len(statements) - 1:
                results.append(None)
                continue
            kind = CATALOG.classify(0, message)
            raise EngineError(message[:1500], internal=kind is ErrorKind.INTERNAL, timeout=kind is ErrorKind.TIMEOUT)
        return results[1:]

    def execute(self, text: str, binds: "dict | None" = None, options: "dict | None" = None) -> list:
        """One query text with the case's bind variables as a `LET` prelude; the last
        statement's rows, `id` as the logical id. A single SELECT gets the session's
        query timeout."""
        lets = "".join(f"LET ${name} = {surql_literal(value)};\n" for name, value in (binds or {}).items())
        body = text.strip().rstrip(";")
        if self.query_timeout_ms and _SELECT_RE.match(body) and ";" not in body and "TIMEOUT" not in body.upper():
            body = f"{body} TIMEOUT {self.query_timeout_ms}ms"
        results = self.query(lets + body + ";")
        answer = results[-1] if results else []
        rows = answer if isinstance(answer, list) else [answer]
        return [_strip(r) for r in rows]

    def explain(self, text: str, binds: "dict | None" = None) -> "list | None":
        """The plan of a single SELECT (`EXPLAIN`), or None."""
        body = text.strip().rstrip(";")
        if not _SELECT_RE.match(body) or ";" in body:
            return None
        lets = "".join(f"LET ${name} = {surql_literal(value)};\n" for name, value in (binds or {}).items())
        try:
            plan = self.query(lets + body + " EXPLAIN;")[-1]
        except EngineError:
            return None
        # each step an object (`operation`, `detail`): the fingerprint is hashable text
        return [json.dumps(step, sort_keys=True, default=str) for step in (plan if isinstance(plan, list) else [plan])]

    # --- setup and the state check -------------------------------------------------------

    def apply_setup(self, plan: list) -> None:
        for step in plan:
            self.apply_step(step)

    def apply_step(self, step) -> None:
        if isinstance(step, Surql):
            self.query(step.text)
        else:
            raise TypeError(f"not a SurrealDB setup step: {type(step).__name__}")

    def _by_ids(self, select: str, column: str, ids: list) -> list:
        rows: list = []
        ids = sorted(set(ids))
        for k in range(0, len(ids), _IN_CHUNK):
            chunk = ", ".join(engine_id(i) for i in ids[k : k + _IN_CHUNK])
            rows += self.query(f"{select} WHERE {column} IN [{chunk}];")[-1]
        return rows

    def read_nodes(self, table: str, ids: "list | None" = None) -> dict:
        select = f"SELECT * FROM {table}"
        rows = self.query(select + ";")[-1] if ids is None else self._by_ids(select, "id", list(ids))
        out: dict = {}
        for r in rows:
            lid = logical_id(r.get("id"))
            if lid is not None:
                out[lid] = {k: v for k, v in r.items() if k != "id"}
        return out

    def count_nodes(self, table: str) -> int:
        rows = self.query(f"SELECT count() FROM {table} GROUP ALL;")[-1]
        return int(rows[0]["count"]) if rows else 0

    @staticmethod
    def _attrs(et, row: dict) -> dict:
        return {f.name: row.get(f.name) for f in et.fields if f.name in row and row.get(f.name) is not None}

    def read_edges(self, data_model: str, instance, et, sources: "list | None" = None) -> list:
        """[(eid, from id, to id, {attrs})] as the data model stores them; with
        `sources`, those leaving the given records only."""
        out = []
        if data_model == "graph":
            select = f"SELECT * FROM {et.name}"
            rows = self.query(select + ";")[-1] if sources is None else self._by_ids(select, "in", list(sources))
            for r in rows:
                out.append((logical_id(r.get("id")), logical_id(r.get("in")), logical_id(r.get("out")), self._attrs(et, r)))
        elif data_model == "rel":
            select = f"SELECT * FROM {et.name}"
            rows = self.query(select + ";")[-1] if sources is None else self._by_ids(select, "src", list(sources))
            for r in rows:
                out.append((logical_id(r.get("id")), logical_id(r.get("src")), logical_id(r.get("dst")), self._attrs(et, r)))
        else:
            select = f"SELECT id, {et.name} AS links FROM {et.from_type}"
            rows = self.query(select + ";")[-1] if sources is None else self._by_ids(select, "id", list(sources))
            for r in rows:
                for link in r.get("links") or []:
                    if isinstance(link, dict):
                        out.append((f"{et.name}/{link.get('eid')}", logical_id(r.get("id")), logical_id(link.get("target")), self._attrs(et, link)))
        return out

    def count_edges(self, data_model: str, instance, et) -> int:
        if data_model == "doc":
            v = self.query(f"RETURN math::sum((SELECT VALUE array::len({et.name} ?? []) FROM {et.from_type}));")[-1]
            return int(v or 0)
        rows = self.query(f"SELECT count() FROM {et.name} GROUP ALL;")[-1]
        return int(rows[0]["count"]) if rows else 0

    def ping(self):
        """One trivial query (`ganda surrealdb doctor`)."""
        return self.execute("RETURN 1")


class SurrealServer(DockerServer):
    engine = "surrealdb"
    image = os.environ.get("SURREAL_IMAGE") or DEFAULT_IMAGE
    container_port = 8000
    url_env = "SURREAL_URL"

    def __init__(self, image: "str | None" = None, memory_limit: "str | None" = None, query_timeout_ms: int = DEFAULT_TIMEOUT_MS):
        super().__init__(image, memory_limit)
        self.query_timeout_ms = query_timeout_ms

    def environment(self) -> dict:
        # the `/sql` body bound (its default, 1 MiB, refuses a large record's statement)
        return {"SURREAL_HTTP_MAX_SQL_BODY_SIZE": f"{HTTP_MAX_SQL_BODY_SIZE // (1024 * 1024)} MiB"}

    def command(self) -> list:
        return ["start", "--user", DEFAULT_USER, "--pass", DEFAULT_PASSWORD, "memory"]

    def is_ready(self) -> bool:
        return requests.get(f"{self.base_url}/health", timeout=1.0).status_code == 200

    def _credentials(self) -> tuple:
        if self.external:
            return os.environ.get("SURREAL_USER") or DEFAULT_USER, os.environ.get("SURREAL_PASSWORD") or DEFAULT_PASSWORD
        return DEFAULT_USER, DEFAULT_PASSWORD

    def version(self) -> "str | None":
        try:
            text = requests.get(f"{self.base_url}/version", timeout=5).text.strip()
        except requests.exceptions.RequestException:
            return None
        return text.removeprefix("surrealdb-") or None

    def session(self, database: str) -> SurrealSession:
        user, password = self._credentials()
        return SurrealSession(self.base_url, database, user, password, timeout=max(120.0, self.query_timeout_ms / 1000 + 60), query_timeout_ms=self.query_timeout_ms)

    def fresh_session(self, database: str) -> SurrealSession:
        s = self.session(database)
        s.query(f"REMOVE DATABASE IF EXISTS {database}; DEFINE DATABASE {database};")
        return s

    def drop_database(self, database: str) -> None:
        self.session(database).query(f"REMOVE DATABASE IF EXISTS {database};")

"""Lifecycle management for an OrientDB server under test, plus a thin HTTP client.

OrientDB is driven as a real server (Docker, `orientdb:latest`) over its REST API, so
the fuzzer exercises the path a real client would: `/command/<db>/sql` for one
statement with named parameters, `/command/<db>/script` for a multi-statement script,
`/batch/<db>` for a transactional batch. Every representation of one instance lives on
the same server in its own database (`fz_<ns>_doc` / `_graph` / `_rel`), so a mismatch
is attributable to the engine, never to the environment.

Three error classes:
- `OrientError`: the server understood the request and rejected it (a parse error, a
  validation failure, a duplicate key). A finding when it happens on one side and
  not another. `internal` marks the rejections whose body is a raw Java exception
  (`NullPointerException`, `ClassCastException`, an HTTP 501 `Unsupported
  OperationException`) -- the engine crashed *inside* the request; those are counted
  and bundled even when every data model agrees on them.
- `OrientCrash`: the server could not be talked to at all (connection refused/reset,
  garbage instead of JSON). The campaign restarts the server.
- a TIMEOUT (`OrientError(timeout=True)`): the engine gave up on the statement (the
  `TIMEOUT ... EXCEPTION` clause every SELECT carries, `OTimeoutException`), or the
  transport did (an HTTP read timeout while the server still answers a ping). No
  answer, not a rejection: the oracle leaves the variant out.

**Large data.** The server is started with `network.http.maxLength` raised to the
declared request bound (`ganda.orientdb.engine.limits.HTTP_MAX_LENGTH`; the image's default is
1 MB) and, at the large scale, a bigger heap (`LARGE_SCALE_MEMORY`). The state check
reads counts and samples through `count_nodes` / `read_nodes(ids=...)` /
`read_edges(sources=...)`, and the doc data model's link targets are resolved in bulk
(`@rid IN [...]`) rather than one query per link.

OrientDB answers most rejections with HTTP 500 and a JSON `errors` body, so the status
code alone does not separate a rejection from a crash: a reply that parses as JSON is
a rejection, one that does not (or none) is a crash.

**RID capture.** A plan step that inserts a record names the logical id it creates
(`Sql.capture`); the record the command returns carries `@rid`, and the session keeps
`rids[id] = "#c:p"` so a later step's `Ref(id)` parameter resolves to it. A script or
batch that folds several inserts answers with only the last command's result, so the
session re-reads `SELECT id, @rid` for the ids those steps captured.
"""
from __future__ import annotations

import itertools
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from ganda.orientdb.storage.mapping.common import Batch, Script, Sql, resolve_refs
from ganda.core.engine.provider import EngineCrash, EngineError, Server, Session


class OrientError(EngineError):
    """The server rejected a request."""


class OrientCrash(EngineCrash):
    """The server could not be reached or replied with garbage."""


DEFAULT_IMAGE = "orientdb:latest"
DEFAULT_PASSWORD = "fuzz"
CONTAINER_LABEL = "orientdb-fuzzer=true"
DEFAULT_TIMEOUT_MS = 30000
# the JVM heap at the large scale (`ORIENTDB_OPTS_MEMORY`; the image's default is 2 GB)
LARGE_SCALE_MEMORY = "-Xms1G -Xmx3G"
# how many ids one `IN :ids` lookup carries
_IN_CHUNK = 500
_TIMEOUT_RE = re.compile(r"OTimeoutException|OCommandInterruptedException")

# A rejection body that is a raw Java exception rather than one of OrientDB's own
# command/validation exceptions: the request reached code that did not expect it.
_INTERNAL_RE = re.compile(
    r"java\.lang\.(NullPointer|ClassCast|UnsupportedOperation|NumberFormat|IllegalArgument|IllegalState|IndexOutOfBounds|ArrayIndexOutOfBounds|StackOverflow|OutOfMemory)"
    r"|java\.util\.(ConcurrentModification|NoSuchElement)"
)
_ORIENT_EXPECTED_RE = re.compile(r"OCommandSQLParsingException|OCommandExecutionException|OValidationException|ORecordDuplicatedException|OConcurrentModificationException|ORecordNotFoundException|OTimeoutException|OCommandInterruptedException|OSchemaException|OIndexException|OConfigurationException|ODatabaseException|OSecurityException")


def classify_error(status: int, content: str) -> bool:
    """True when the rejection is an internal error (see module docstring)."""
    if status == 501:
        return True
    if _INTERNAL_RE.search(content) and not _ORIENT_EXPECTED_RE.search(content.split("\n")[0]):
        return True
    return False


_VOLATILE_KEYS = ("@rid", "@class", "@version", "@type", "@fieldTypes")


def strip_meta(row: Any) -> Any:
    """Drop OrientDB's record metadata and the `_$$$ORDER_BY_ALIAS$$$_n` columns the
    executor adds when ordering by a non-projected field."""
    if isinstance(row, dict):
        return {k: strip_meta(v) for k, v in row.items() if k not in _VOLATILE_KEYS and not k.startswith("_$$$")}
    if isinstance(row, list):
        return [strip_meta(v) for v in row]
    return row


def is_script(text: str) -> bool:
    return text.lstrip().upper().startswith("BEGIN;")


def unwrap_script(result: list) -> list:
    """A script's `RETURN $r` comes back as `[{"value": [rows...]}]` (probed); the
    rows are the answer."""
    if len(result) == 1 and isinstance(result[0], dict) and set(result[0]) == {"value"}:
        v = result[0]["value"]
        return [strip_meta(r) if isinstance(r, dict) else r for r in v] if isinstance(v, list) else [v]
    return result


class OrientSession(Session):
    """Talks to one database on a running server. One `requests.Session` per database:
    the server's session cookie is bound to a database, and a cookie left over from a
    dropped database answers 401 for every later request (probed)."""

    def __init__(self, base_url: str, password: str, db: str, user: str = "root", timeout: float = 120.0, query_timeout_ms: int = DEFAULT_TIMEOUT_MS):
        self.base_url = base_url.rstrip("/")
        self.db = db
        self.timeout = timeout
        self.query_timeout_ms = query_timeout_ms
        self._session = requests.Session()
        self._session.auth = (user, password)
        self._session.headers.update({"Accept": "application/json"})
        self.rids: dict = {}

    # --- transport ---------------------------------------------------------------

    def _request(self, method: str, path: str, body=None, ok=(200, 201, 202, 204)) -> Any:
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.request(method, url, json=body, timeout=self.timeout)
        except requests.exceptions.Timeout as e:
            if self._alive():
                raise OrientError(f"{method} {path} gave no answer within {self.timeout:.0f}s (the server still answers)", timeout=True) from e
            raise OrientCrash(f"could not reach OrientDB at {self.base_url}: {e}") from e
        except requests.exceptions.RequestException as e:
            raise OrientCrash(f"could not reach OrientDB at {self.base_url}: {e}") from e
        if resp.status_code == 204:
            return None
        try:
            payload = resp.json()
        except ValueError as e:
            if resp.status_code >= 500 and "errors" not in resp.text:
                raise OrientCrash(f"OrientDB {resp.status_code} non-JSON on {method} {path}: {resp.text[:300]}") from e
            raise OrientError(f"{method} {path} answered {resp.status_code}: {resp.text[:500]}", status=resp.status_code)
        if resp.status_code not in ok:
            content = resp.text[:2000]
            if isinstance(payload, dict) and payload.get("errors"):
                content = "; ".join(str(e.get("content", e)) for e in payload["errors"])
            raise OrientError(f"{method} {path} rejected ({resp.status_code}): {content[:1500]}", internal=classify_error(resp.status_code, content), status=resp.status_code, timeout=bool(_TIMEOUT_RE.search(content)))
        return payload

    def _alive(self) -> bool:
        """Whether the server answers at all (a read timeout with a live server is a
        slow statement, not a crash)."""
        try:
            return requests.get(f"{self.base_url}/listDatabases", auth=self._session.auth, timeout=5).status_code == 200
        except requests.exceptions.RequestException:
            return False

    # --- commands -------------------------------------------------------------------

    def sql(self, command: str, params: "dict | None" = None, timeout_ms: "int | None" = None) -> list:
        """One SQL statement with named parameters; the whole result list, metadata
        stripped. A read query gets a `TIMEOUT ... EXCEPTION` clause (the session's
        `query_timeout_ms` unless given; 0 for none) so a runaway plan is a timeout
        rather than a hung worker."""
        if timeout_ms is None:
            timeout_ms = self.query_timeout_ms
        text = command
        # MATCH and TRAVERSE take no TIMEOUT clause (parse error, probed); only a SELECT gets one
        if timeout_ms and text.lstrip().upper().startswith("SELECT") and " TIMEOUT " not in text.upper():
            text = f"{text} TIMEOUT {timeout_ms} EXCEPTION"
        body: dict = {"command": text}
        if params:
            body["parameters"] = params
        payload = self._request("POST", f"/command/{self.db}/sql", body)
        return [strip_meta(r) for r in payload.get("result", [])]

    def sql_raw(self, command: str, params: "dict | None" = None) -> list:
        body: dict = {"command": command}
        if params:
            body["parameters"] = params
        return self._request("POST", f"/command/{self.db}/sql", body).get("result", [])

    def script(self, text: str, params: "dict | None" = None) -> list:
        body: dict = {"command": text}
        if params:
            body["parameters"] = params
        payload = self._request("POST", f"/command/{self.db}/script", body)
        return [strip_meta(r) for r in payload.get("result", [])]

    def batch(self, steps: list, transaction: bool = True) -> Any:
        """The `/batch` endpoint takes POSITIONAL parameters only (a named map is a
        ClassCastException, probed), so every `:name` is rewritten to `?` in order."""
        ops = []
        for s in steps:
            text, values = positional(s.command, resolve_refs(s.params, self.rids))
            op = {"type": "cmd", "language": "sql", "command": text}
            if values:
                op["parameters"] = values
            ops.append(op)
        return self._request("POST", f"/batch/{self.db}", {"transaction": transaction, "operations": ops})

    def execute(self, text: str, params: "dict | None" = None, options: "dict | None" = None) -> list:
        """One query text of a case: a script (`BEGIN; ...`) through the script door,
        its `RETURN` unwrapped; anything else as one SQL statement."""
        if is_script(text):
            return unwrap_script(self.script(text, params))
        return self.sql(text, params)

    def explain(self, command: str, params: "dict | None" = None) -> "list | None":
        """The execution plan's step types -- a plan fingerprint (`FetchFromIndexStep`,
        `FetchFromClassExecutionStep`, `FilterStep`, ...), index names kept. None for a
        script (EXPLAIN takes one statement)."""
        if is_script(command):
            return None
        body: dict = {"command": f"EXPLAIN {command}"}
        if params:
            body["parameters"] = params
        payload = self._request("POST", f"/command/{self.db}/sql", body)
        out = []
        for r in payload.get("result", []):
            plan = r.get("executionPlan", {})
            out.extend(_walk_steps(plan.get("steps", [])))
        return out

    # --- setup plans -------------------------------------------------------------------

    def apply_setup(self, plan: list) -> None:
        for step in plan:
            self.apply_step(step)

    def apply_step(self, step) -> None:
        if isinstance(step, Sql):
            rows = self.sql_raw(step.command, resolve_refs(step.params, self.rids))
            if step.capture:
                rid = _first_rid(rows)
                if rid is None:
                    raise OrientError(f"no @rid in the result of {step.command[:80]!r} for {step.capture}")
                self.rids[step.capture] = rid
        elif isinstance(step, Script):
            self.script(step.text, resolve_refs(step.params, self.rids))
            self._refresh_rids(step.captures.values())
        elif isinstance(step, Batch):
            self.batch(step.steps, step.transaction)
            self._refresh_rids([s.capture for s in step.steps if isinstance(s, Sql) and s.capture])
        else:
            raise TypeError(f"unknown setup step {step!r}")

    def _refresh_rids(self, ids) -> None:
        by_class: dict = {}
        for rid_id in ids:
            by_class.setdefault(rid_id.split("/", 1)[0], []).append(rid_id)
        for cls, wanted in by_class.items():
            for rows in self._by_ids(f"SELECT id, @rid FROM {cls}", "id", wanted, raw=True):
                for r in rows:
                    self.rids[r["id"]] = r["@rid"]
            # an id the same script inserted AND deleted (a churned-away node) is
            # legitimately absent; a later `Ref` to it raises with a clear message

    def _by_ids(self, select: str, column: str, ids: list, raw: bool = False):
        """`select` restricted to `column IN :ids`, `_IN_CHUNK` ids at a time (one
        lookup per chunk, however many ids there are)."""
        for k in range(0, len(ids), _IN_CHUNK):
            chunk = list(ids[k : k + _IN_CHUNK])
            text = f"{select} WHERE {column} IN :ids"
            yield self.sql_raw(text, {"ids": chunk}) if raw else self.sql(text, {"ids": chunk}, timeout_ms=0)

    # --- reading the stored state back (`ganda.core.engine.state_check`) ----------------------

    def read_nodes(self, table: str, ids: "list | None" = None) -> dict:
        if ids is None:
            rows = self.sql(f"SELECT FROM {table}", timeout_ms=0)
        else:
            rows = [r for chunk in self._by_ids(f"SELECT FROM {table}", "id", list(ids)) for r in chunk]
        return {r["id"]: r for r in rows if "id" in r}

    def count_nodes(self, table: str) -> int:
        return int(self.sql(f"SELECT count(*) AS v FROM {table}", timeout_ms=0)[0]["v"])

    def _resolve_targets(self, to_type: str, targets: list) -> dict:
        """{RID string: logical id} for the link targets, `_IN_CHUNK` at a time; a
        dangling link (its record deleted) is absent."""
        out: dict = {}
        rids = sorted({t for t in targets if isinstance(t, str) and t.startswith("#")})
        for k in range(0, len(rids), _IN_CHUNK):
            chunk = rids[k : k + _IN_CHUNK]
            for r in self.sql_raw(f"SELECT id, @rid FROM {to_type} WHERE @rid IN :r", {"r": chunk}):
                out[r["@rid"]] = r.get("id")
        return out

    def read_edges(self, data_model: str, instance, et, sources: "list | None" = None) -> list:
        """[(eid, from id, to id, {attrs})] as the data model stores them; with
        `sources`, those leaving the given records only."""
        out = []
        if data_model == "graph":
            select = f"SELECT eid, out.id AS s, in.id AS t, * FROM {et.name}"
            rows = self.sql(select, timeout_ms=0) if sources is None else [r for chunk in self._by_ids(select, "out.id", list(sources)) for r in chunk]
            for r in rows:
                out.append((r.get("eid"), r.get("s"), r.get("t"), {f.name: r.get(f.name) for f in et.fields if f.name in r and r.get(f.name) is not None}))
        elif data_model == "rel":
            select = f"SELECT FROM {et.name}"
            rows = self.sql(select, timeout_ms=0) if sources is None else [r for chunk in self._by_ids(select, "src_id", list(sources)) for r in chunk]
            for r in rows:
                out.append((r.get("eid"), r.get("src_id"), r.get("dst_id"), {f.name: r.get(f.name) for f in et.fields if f.name in r and r.get(f.name) is not None}))
        else:
            select = f"SELECT id, {et.name} AS links FROM {et.from_type}"
            rows = self.sql(select, timeout_ms=0) if sources is None else [r for chunk in self._by_ids(select, "id", list(sources)) for r in chunk]
            resolved = self._resolve_targets(et.to_type, [link.get("target") for r in rows for link in (r.get("links") or []) if isinstance(link, dict)])
            for r in rows:
                for link in r.get("links") or []:
                    target = link.get("target")
                    tid = None
                    if isinstance(target, str) and target.startswith("#"):
                        tid = resolved.get(target)
                    elif isinstance(target, dict):
                        tid = target.get("id")
                    out.append((link.get("eid"), r["id"], tid, {f.name: link.get(f.name) for f in et.fields if f.name in link and link.get(f.name) is not None}))
        return out

    def count_edges(self, data_model: str, instance, et) -> int:
        if data_model == "doc":
            # an absent list has size 0 (probed); a sum over no rows is 0 or nothing
            rows = self.sql(f"SELECT sum({et.name}.size()) AS v FROM {et.from_type}", timeout_ms=0)
            return int(rows[0].get("v") or 0) if rows else 0
        return int(self.sql(f"SELECT count(*) AS v FROM {et.name}", timeout_ms=0)[0]["v"])

    def ping(self):
        """One trivial query (`ganda orientdb doctor`)."""
        return self.sql("SELECT 1 AS ok")

    def classes(self) -> list:
        rows = self.sql_raw("SELECT name FROM (SELECT expand(classes) FROM metadata:schema) WHERE name NOT LIKE 'O%' AND name NOT IN ['V', 'E']")
        return sorted(r["name"] for r in rows)


_PARAM_RE = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)")


def positional(command: str, params: dict) -> "tuple[str, list]":
    """Rewrite `:name` references to `?` in order of appearance; a name used twice
    contributes its value twice."""
    values: list = []

    def sub(m):
        name = m.group(1)
        if name not in params:
            return m.group(0)
        values.append(params[name])
        return "?"

    return _PARAM_RE.sub(sub, command), values


def _walk_steps(steps: list) -> list:
    out = []
    for st in steps:
        t = st.get("type") or st.get("name")
        desc = st.get("description", "")
        m = re.search(r"FETCH FROM INDEX (\S+)", desc)
        if m:
            t += f"[{m.group(1)}]"
        out.append(t)
        out.extend(_walk_steps(st.get("subSteps", [])))
    return out


def _first_rid(rows: list) -> "str | None":
    for r in rows:
        if isinstance(r, dict) and r.get("@rid"):
            return r["@rid"]
    return None


_container_ids: list = []
_name_counter = itertools.count()


def _parse_docker_port_output(output: str) -> int:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"docker port produced no output to parse: {output!r}")
    for line in lines:
        if line.startswith("0.0.0.0:") or line.startswith("127.0.0.1:"):
            return int(line.rsplit(":", 1)[-1])
    return int(lines[0].rsplit(":", 1)[-1])


@dataclass
class OrientServer(Server):
    """A running OrientDB server. Use as a context manager.

    Launched via Docker by default. Set `url` (or `ORIENT_URL`) to attach to an
    already-running server instead, with `password` (or `ORIENT_PASSWORD`) for its
    root user. Containers carry the label `orientdb-fuzzer=true` and are removed with
    `docker rm -f` on stop, never started with `--rm` -- a crashed container's logs are
    what `diagnostics()` needs.

    `coverage=True` mounts the JaCoCo agent into the container and starts the JVM with
    it (`JAVA_OPTS`, which `server.sh` passes through), its TCP port published next to
    the REST one; an external server's agent is `ORIENT_JACOCO=host:port`.

    `http_max_length` is the request body bound the server is started with
    (`network.http.maxLength`, through `ORIENTDB_SETTINGS`); `memory` the JVM heap
    (`ORIENTDB_OPTS_MEMORY`); `query_timeout_ms` every session's `TIMEOUT` clause, with
    the HTTP read timeout a minute past it. An external server keeps its own settings."""

    image: str = DEFAULT_IMAGE
    password: str = DEFAULT_PASSWORD
    url: "str | None" = None
    memory_limit: "str | None" = None
    startup_timeout: float = 120.0
    coverage: bool = False  # run the server JVM under the JaCoCo agent (`ganda.orientdb.engine.coverage`)
    query_timeout_ms: int = DEFAULT_TIMEOUT_MS
    http_max_length: "int | None" = None
    memory: "str | None" = None
    container_id: "str | None" = field(default=None, init=False)
    container_name: "str | None" = field(default=None, init=False)
    base_url: str = field(default="", init=False)
    coverage_port: "int | None" = field(default=None, init=False)

    def __enter__(self) -> "OrientServer":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def external(self) -> bool:
        return bool(self.url or os.environ.get("ORIENT_URL"))

    def start(self) -> None:
        if self.external:
            self.base_url = (self.url or os.environ["ORIENT_URL"]).rstrip("/")
            self.password = self.password if self.url else os.environ.get("ORIENT_PASSWORD", self.password)
            self._wait_ready()
            return
        self.container_name = f"orientdb-fuzzer-{os.getpid()}-{next(_name_counter)}"
        cmd = [
            "docker", "run", "-d",
            "--name", self.container_name,
            "--label", CONTAINER_LABEL,
            "-e", f"ORIENTDB_ROOT_PASSWORD={self.password}",
            "-p", "127.0.0.1:0:2480",
        ]
        if self.memory_limit:
            cmd += ["--memory", self.memory_limit]
        if self.http_max_length:
            cmd += ["-e", f"ORIENTDB_SETTINGS=-Dnetwork.http.maxLength={self.http_max_length}"]
        if self.memory:
            cmd += ["-e", f"ORIENTDB_OPTS_MEMORY={self.memory}"]
        if self.coverage:
            from ganda.orientdb.engine import coverage

            cmd += [
                "-v", f"{coverage.agent_jar()}:{coverage.AGENT_IN_CONTAINER}:ro",
                "-e", f"JAVA_OPTS={coverage.agent_java_opts()}",
                "-p", f"127.0.0.1:0:{coverage.AGENT_PORT}",
            ]
        cmd.append(self.image)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise OrientCrash(f"docker run failed: {proc.stderr.strip()}")
        self.container_id = proc.stdout.strip()
        _container_ids.append(self.container_id)
        port_out = subprocess.run(["docker", "port", self.container_name, "2480/tcp"], capture_output=True, text=True)
        port = _parse_docker_port_output(port_out.stdout)
        self.base_url = f"http://127.0.0.1:{port}"
        if self.coverage:
            from ganda.orientdb.engine import coverage

            cov_out = subprocess.run(["docker", "port", self.container_name, f"{coverage.AGENT_PORT}/tcp"], capture_output=True, text=True)
            self.coverage_port = _parse_docker_port_output(cov_out.stdout)
        self._wait_ready()

    def coverage_address(self) -> "tuple[str, int]":
        """Where the JaCoCo agent listens (`ganda.orientdb.engine.coverage.dump`)."""
        if self.external:
            spec = os.environ.get("ORIENT_JACOCO")
            if not spec:
                raise RuntimeError("an external server's coverage needs ORIENT_JACOCO=host:port")
            host, _, port = spec.rpartition(":")
            return host or "127.0.0.1", int(port)
        if self.coverage_port is None:
            raise RuntimeError("the server was not started with coverage=True")
        return "127.0.0.1", self.coverage_port

    def _wait_ready(self) -> None:
        deadline = time.time() + self.startup_timeout
        last = ""
        while time.time() < deadline:
            try:
                r = requests.get(f"{self.base_url}/listDatabases", auth=("root", self.password), timeout=3)
                if r.status_code == 200:
                    return
                last = f"{r.status_code}: {r.text[:200]}"
            except requests.exceptions.RequestException as e:
                last = str(e)
            time.sleep(0.5)
        raise RuntimeError(f"OrientDB at {self.base_url} did not become ready: {last}")

    def stop(self) -> None:
        if self.container_id is None:
            return
        subprocess.run(["docker", "rm", "-f", self.container_id], capture_output=True, text=True)
        if self.container_id in _container_ids:
            _container_ids.remove(self.container_id)
        self.container_id = None

    def diagnostics(self) -> str:
        if self.container_id is None:
            return "(external server: no container diagnostics)"
        try:
            insp = subprocess.run(
                ["docker", "inspect", "-f", "status={{.State.Status}} exitcode={{.State.ExitCode}} oomkilled={{.State.OOMKilled}}", self.container_id],
                capture_output=True, text=True, timeout=10,
            )
            logs = subprocess.run(["docker", "logs", "--tail", "80", self.container_id], capture_output=True, text=True, timeout=10)
            return insp.stdout.strip() + "\n--- log tail ---\n" + (logs.stdout + logs.stderr)[-8000:]
        except Exception as e:  # noqa: BLE001
            return f"(diagnostics failed: {e})"

    # --- databases -----------------------------------------------------------------

    def _root(self) -> requests.Session:
        s = requests.Session()
        s.auth = ("root", self.password)
        return s

    def create_database(self, name: str) -> None:
        r = self._root().post(f"{self.base_url}/database/{name}/plocal", timeout=60)
        if r.status_code not in (200, 201):
            raise OrientError(f"create database {name} answered {r.status_code}: {r.text[:300]}", status=r.status_code)

    def drop_database(self, name: str) -> None:
        try:
            self._root().delete(f"{self.base_url}/database/{name}", timeout=60)
        except requests.exceptions.RequestException as e:
            raise OrientCrash(str(e)) from e

    def databases(self) -> list:
        r = self._root().get(f"{self.base_url}/listDatabases", timeout=30)
        return sorted(r.json().get("databases", []))

    def session(self, db: str) -> OrientSession:
        return OrientSession(self.base_url, self.password, db=db, timeout=max(120.0, self.query_timeout_ms / 1000.0 + 60.0), query_timeout_ms=self.query_timeout_ms)


    def fresh_session(self, db: str) -> OrientSession:
        """Drop-and-create `db`, then return a session on it."""
        self.drop_database(db)
        self.create_database(db)
        return self.session(db)


def cleanup_orphans() -> None:
    for cid in list(_container_ids):
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True, text=True)
        _container_ids.remove(cid)


def db_name(ns: str, data_model: str) -> str:
    return f"fz_{ns}_{data_model}"

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
  and bundled even when every side agrees on them.
- `OrientCrash`: the server could not be talked to at all (connection refused/reset,
  timeout, garbage instead of JSON). The campaign restarts the server.

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

from ganda.orientdb.materialize.common import Batch, Script, Sql, resolve_refs


class OrientError(RuntimeError):
    """The server rejected a request."""

    def __init__(self, message: str, internal: bool = False, status: int = 0):
        super().__init__(message)
        self.internal = internal
        self.status = status


class OrientCrash(RuntimeError):
    """The server could not be reached or replied with garbage."""


DEFAULT_IMAGE = "orientdb:latest"
DEFAULT_PASSWORD = "fuzz"
CONTAINER_LABEL = "orientdb-fuzzer=true"
DEFAULT_TIMEOUT_MS = 30000

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


class OrientSession:
    """Talks to one database on a running server. One `requests.Session` per database:
    the server's session cookie is bound to a database, and a cookie left over from a
    dropped database answers 401 for every later request (probed)."""

    def __init__(self, base_url: str, password: str, db: str, user: str = "root", timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.db = db
        self.timeout = timeout
        self._session = requests.Session()
        self._session.auth = (user, password)
        self._session.headers.update({"Accept": "application/json"})
        self.rids: dict = {}

    # --- transport ---------------------------------------------------------------

    def _request(self, method: str, path: str, body=None, ok=(200, 201, 202, 204)) -> Any:
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.request(method, url, json=body, timeout=self.timeout)
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
            raise OrientError(f"{method} {path} rejected ({resp.status_code}): {content[:1500]}", internal=classify_error(resp.status_code, content), status=resp.status_code)
        return payload

    # --- commands -------------------------------------------------------------------

    def sql(self, command: str, params: "dict | None" = None, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> list:
        """One SQL statement with named parameters; the whole result list, metadata
        stripped. A read query gets a `TIMEOUT ... EXCEPTION` clause so a runaway plan
        is a rejection rather than a hung worker."""
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

    def explain(self, command: str, params: "dict | None" = None) -> list:
        """The execution plan's step types -- a plan fingerprint (`FetchFromIndexStep`,
        `FetchFromClassExecutionStep`, `FilterStep`, ...), index names kept."""
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
            rows = self.sql_raw(f"SELECT id, @rid FROM {cls} WHERE id IN :ids", {"ids": wanted})
            for r in rows:
                self.rids[r["id"]] = r["@rid"]
            # an id the same script inserted AND deleted (a churned-away node) is
            # legitimately absent; a later `Ref` to it raises with a clear message

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
class OrientInstance:
    """A running OrientDB server. Use as a context manager.

    Launched via Docker by default. Set `url` (or `ORIENT_URL`) to attach to an
    already-running server instead, with `password` (or `ORIENT_PASSWORD`) for its
    root user. Containers carry the label `orientdb-fuzzer=true` and are removed with
    `docker rm -f` on stop, never started with `--rm` -- a crashed container's logs are
    what `diagnostics()` needs.

    `coverage=True` mounts the JaCoCo agent into the container and starts the JVM with
    it (`JAVA_OPTS`, which `server.sh` passes through), its TCP port published next to
    the REST one; an external server's agent is `ORIENT_JACOCO=host:port`."""

    image: str = DEFAULT_IMAGE
    password: str = DEFAULT_PASSWORD
    url: "str | None" = None
    memory_limit: "str | None" = None
    startup_timeout: float = 120.0
    coverage: bool = False  # run the server JVM under the JaCoCo agent (`ganda.orientdb.coverage`)
    container_id: "str | None" = field(default=None, init=False)
    container_name: "str | None" = field(default=None, init=False)
    base_url: str = field(default="", init=False)
    coverage_port: "int | None" = field(default=None, init=False)

    def __enter__(self) -> "OrientInstance":
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
        if self.coverage:
            from ganda.orientdb import coverage

            cmd += [
                "-v", f"{coverage.agent_jar()}:{coverage.AGENT_IN_CONTAINER}:ro",
                "-e", f"JAVA_OPTS={coverage.agent_java_opts()}",
                "-p", f"127.0.0.1:0:{coverage.AGENT_PORT}",
            ]
        cmd.append(self.image)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"docker run failed: {proc.stderr.strip()}")
        self.container_id = proc.stdout.strip()
        _container_ids.append(self.container_id)
        port_out = subprocess.run(["docker", "port", self.container_name, "2480/tcp"], capture_output=True, text=True)
        port = _parse_docker_port_output(port_out.stdout)
        self.base_url = f"http://127.0.0.1:{port}"
        if self.coverage:
            from ganda.orientdb import coverage

            cov_out = subprocess.run(["docker", "port", self.container_name, f"{coverage.AGENT_PORT}/tcp"], capture_output=True, text=True)
            self.coverage_port = _parse_docker_port_output(cov_out.stdout)
        self._wait_ready()

    def coverage_address(self) -> "tuple[str, int]":
        """Where the JaCoCo agent listens (`ganda.orientdb.coverage.dump`)."""
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
        return OrientSession(self.base_url, self.password, db=db)

    def fresh_session(self, db: str) -> OrientSession:
        """Drop-and-create `db`, then return a session on it."""
        self.drop_database(db)
        self.create_database(db)
        return self.session(db)


def cleanup_orphans() -> None:
    for cid in list(_container_ids):
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True, text=True)
        _container_ids.remove(cid)


def db_name(ns: str, side: str) -> str:
    return f"fz_{ns}_{side}"

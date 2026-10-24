"""SurrealDB's connection layer: a server in Docker and one HTTP session per database.

Statements go to `POST /sql` as text. A session selects its namespace and database
with a `USE` preamble on every request (the server keeps no session state over HTTP)
and drops that statement's result. Every data model of a seed is a DATABASE of the one
namespace `ganda`.

SurrealQL takes values as literals, so the provider's concrete syntax will interpolate
them (the legacy port's `surql_literal`); `execute` already accepts the case's bind
variables and sends them as `LET` statements for the texts that use `$name`.
"""
from __future__ import annotations

import json
import os

import requests

from ganda.core.engine.docker import DockerServer, cleanup_orphans  # noqa: F401
from ganda.core.engine.provider import EngineCrash, EngineError, Session

NAMESPACE = "ganda"


class SurrealSession(Session):
    def __init__(self, base_url: str, database: str, user: str = "root", password: str = "root", timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.database = database
        self.timeout = timeout
        self._http = requests.Session()
        self._http.auth = (user, password)
        self._http.headers.update({"Accept": "application/json", "Content-Type": "text/plain"})

    def query(self, surql: str) -> list:
        """Every statement's result, in order; `EngineError` when one failed."""
        full = f"USE NS {NAMESPACE} DB {self.database};\n{surql}"
        try:
            resp = self._http.post(f"{self.base_url}/sql", data=full.encode("utf-8"), timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            raise EngineCrash(f"could not reach SurrealDB at {self.base_url}: {e}") from e
        if 400 <= resp.status_code < 500:
            raise EngineError(f"request rejected ({resp.status_code}): {resp.text[:500]}", status=resp.status_code)
        if resp.status_code >= 500:
            raise EngineCrash(f"SurrealDB answered {resp.status_code}: {resp.text[:300]}")
        try:
            statements = resp.json()
        except ValueError as e:
            raise EngineCrash(f"malformed response from SurrealDB: {e}") from e
        failed = [s for s in statements if s.get("status") != "OK"]
        if failed:
            raise EngineError(str(failed[0].get("result"))[:1500])
        return [s.get("result") for s in statements[1:]]  # without the USE preamble

    def execute(self, text: str, binds: "dict | None" = None, options: "dict | None" = None) -> list:
        lets = "".join(f"LET ${name} = {json.dumps(value)};\n" for name, value in (binds or {}).items())
        results = self.query(lets + text)
        answer = results[-1] if results else []
        return answer if isinstance(answer, list) else [answer]

    def ping(self):
        return self.execute("RETURN 1")


class SurrealServer(DockerServer):
    engine = "surrealdb"
    image = os.environ.get("SURREAL_IMAGE", "surrealdb/surrealdb:latest")
    container_port = 8000
    url_env = "SURREAL_URL"

    def command(self) -> list:
        return ["start", "--user", "root", "--pass", "root", "memory"]

    def is_ready(self) -> bool:
        return requests.get(f"{self.base_url}/health", timeout=1.0).status_code == 200

    def session(self, database: str) -> SurrealSession:
        return SurrealSession(self.base_url, database)

    def fresh_session(self, database: str) -> SurrealSession:
        s = self.session(database)
        s.query(f"REMOVE DATABASE IF EXISTS {database}; DEFINE DATABASE {database};")
        return s

    def drop_database(self, database: str) -> None:
        self.session(database).query(f"REMOVE DATABASE IF EXISTS {database};")

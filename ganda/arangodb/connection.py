"""ArangoDB's connection layer: a server in Docker and one HTTP session per database.

AQL runs through the cursor API with every value a bind variable (`@p`, `@@coll`), which
is exactly the case's `bind_vars`; a variant's options (`QueryCase.variant_options`: the
optimizer rules of a plan variant) travel as the cursor's `options`. `failOnWarning`
turns a warning (a division by zero, an invalid number) into an error on every data
model alike. Every data model of a seed is its own database.
"""
from __future__ import annotations

import os

import requests

from ganda.core.docker import DockerServer, cleanup_orphans  # noqa: F401
from ganda.core.provider import EngineCrash, EngineError, Session

DEFAULT_PASSWORD = "fuzz"
QUERY_OPTIONS = {"failOnWarning": True}
_VOLATILE_KEYS = ("_rev",)


def _strip(row):
    if isinstance(row, dict):
        return {k: _strip(v) for k, v in row.items() if k not in _VOLATILE_KEYS}
    if isinstance(row, list):
        return [_strip(v) for v in row]
    return row


class ArangoSession(Session):
    def __init__(self, base_url: str, password: str, database: str, user: str = "root", timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.database = database
        self.timeout = timeout
        self._http = requests.Session()
        self._http.auth = (user, password)

    def _request(self, method: str, path: str, body=None, system: bool = False):
        url = f"{self.base_url}{'' if system else '/_db/' + self.database}{path}"
        try:
            resp = self._http.request(method, url, json=body, timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            raise EngineCrash(f"could not reach ArangoDB at {self.base_url}: {e}") from e
        try:
            payload = resp.json()
        except ValueError as e:
            raise EngineCrash(f"ArangoDB {resp.status_code} non-JSON on {method} {path}: {resp.text[:300]}") from e
        if resp.status_code >= 400 or (isinstance(payload, dict) and payload.get("error")):
            message = payload.get("errorMessage", resp.text[:500]) if isinstance(payload, dict) else resp.text[:500]
            # a 5xx the server ANSWERED is an internal error, not a dead server
            raise EngineError(f"{method} {path} rejected ({resp.status_code}): {message}", internal=resp.status_code >= 500, status=resp.status_code)
        return payload

    def execute(self, text: str, binds: "dict | None" = None, options: "dict | None" = None) -> list:
        body = {"query": text, "bindVars": binds or {}, "options": {**QUERY_OPTIONS, **(options or {})}, "batchSize": 10000}
        payload = self._request("POST", "/_api/cursor", body)
        rows = list(payload.get("result", []))
        while payload.get("hasMore"):
            payload = self._request("POST", f"/_api/cursor/{payload['id']}")
            rows.extend(payload.get("result", []))
        return [_strip(r) for r in rows]

    def explain(self, text: str, binds: "dict | None" = None) -> "list | None":
        payload = self._request("POST", "/_api/explain", {"query": text, "bindVars": binds or {}})
        return [node.get("type", "?") + (":" + ",".join(ix.get("name", "") for ix in node["indexes"]) if node.get("indexes") else "") for node in payload.get("plan", {}).get("nodes", [])]

    def ping(self):
        return self.execute("RETURN 1")


class ArangoServer(DockerServer):
    engine = "arangodb"
    image = os.environ.get("ARANGO_IMAGE", "arangodb/arangodb:latest")
    container_port = 8529
    url_env = "ARANGO_URL"

    @property
    def password(self) -> str:
        return os.environ.get("ARANGO_PASSWORD", DEFAULT_PASSWORD) if self.external else DEFAULT_PASSWORD

    def environment(self) -> dict:
        return {"ARANGO_ROOT_PASSWORD": DEFAULT_PASSWORD}

    def is_ready(self) -> bool:
        return requests.get(f"{self.base_url}/_api/version", auth=("root", self.password), timeout=1.0).status_code == 200

    def session(self, database: str) -> ArangoSession:
        return ArangoSession(self.base_url, self.password, database)

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

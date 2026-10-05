"""A server in a Docker container: the lifecycle most providers share.

A subclass names the image, the container port, the environment and the readiness
check, and builds its sessions; this class starts the container on an ephemeral host
port, waits for it, autopsies it after a crash and removes it. Containers carry a label
(`<engine>-fuzzer=true`) and are never started with `--rm`, so a crashed one keeps its
logs until `stop()`; `cleanup_orphans()` removes whatever a process started.

A provider whose engine is reachable at a URL (an environment variable it names) skips
Docker: `external` is then True and the campaign never restarts the server.
"""
from __future__ import annotations

import itertools
import os
import subprocess
import time

from ganda.core.provider import EngineCrash, Server

_container_ids: list = []
_name_counter = itertools.count()


def parse_port(output: str) -> int:
    """The host port of `docker port <name> <port>/tcp`. Some setups print one line per
    address family with DIFFERENT ports; the IPv4 line is the one `127.0.0.1` reaches."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"docker port produced no output: {output!r}")
    for line in lines:
        if line.startswith(("0.0.0.0:", "127.0.0.1:")):
            return int(line.rsplit(":", 1)[-1])
    return int(lines[0].rsplit(":", 1)[-1])


def cleanup_orphans() -> None:
    for cid in list(_container_ids):
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True, text=True)
        _container_ids.remove(cid)


class DockerServer(Server):
    engine = ""  # the container label is `<engine>-fuzzer=true`
    image = ""
    container_port = 0
    url_env = ""  # the environment variable that attaches to a running server instead
    startup_timeout = 120.0

    def __init__(self, image: "str | None" = None, memory_limit: "str | None" = None):
        self.image = image or self.image
        self.memory_limit = memory_limit
        self.container_id: "str | None" = None
        self.container_name: "str | None" = None
        self.base_url = ""

    # --- what a subclass says ---------------------------------------------------------

    def environment(self) -> dict:
        """The container's environment variables."""
        return {}

    def command(self) -> list:
        """Arguments after the image name."""
        return []

    def is_ready(self) -> bool:
        """Whether the server answers at `base_url`."""
        raise NotImplementedError

    # --- lifecycle --------------------------------------------------------------------

    @property
    def external(self) -> bool:
        return bool(self.url_env and os.environ.get(self.url_env))

    def start(self) -> None:
        if self.external:
            self.base_url = os.environ[self.url_env].rstrip("/")
            self._wait_ready()
            return
        self.container_name = f"{self.engine}-fuzzer-{os.getpid()}-{next(_name_counter)}"
        cmd = ["docker", "run", "-d", "--name", self.container_name, "--label", f"{self.engine}-fuzzer=true", "-p", f"127.0.0.1:0:{self.container_port}"]
        for k, v in self.environment().items():
            cmd += ["-e", f"{k}={v}"]
        if self.memory_limit:
            cmd += ["--memory", self.memory_limit]
        try:
            started = subprocess.run(cmd + [self.image] + self.command(), capture_output=True, text=True)
        except FileNotFoundError as e:
            raise EngineCrash("docker is not installed (or not on PATH)") from e
        if started.returncode != 0:
            raise EngineCrash(f"docker run failed: {started.stderr.strip()[:500]}")
        self.container_id = started.stdout.strip()
        _container_ids.append(self.container_id)
        port = subprocess.run(["docker", "port", self.container_name, f"{self.container_port}/tcp"], capture_output=True, text=True)
        self.base_url = f"http://127.0.0.1:{parse_port(port.stdout)}"
        self._wait_ready()

    def _wait_ready(self) -> None:
        deadline = time.time() + self.startup_timeout
        while time.time() < deadline:
            try:
                if self.is_ready():
                    return
            except Exception:  # noqa: BLE001 -- not up yet, whatever the client raised
                pass
            time.sleep(0.5)
        raise EngineCrash(f"{self.engine} did not become ready at {self.base_url} within {self.startup_timeout:.0f}s\n{self.diagnostics()}")

    def stop(self) -> None:
        if self.container_id is None:
            return
        subprocess.run(["docker", "rm", "-f", self.container_id], capture_output=True, text=True)
        if self.container_id in _container_ids:
            _container_ids.remove(self.container_id)
        self.container_id = None

    def diagnostics(self, log_lines: int = 80) -> str:
        if self.container_name is None:
            return f"external server at {self.base_url}: no container to inspect"
        state = subprocess.run(["docker", "inspect", "--format", "status={{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}}", self.container_name], capture_output=True, text=True)
        logs = subprocess.run(["docker", "logs", "--tail", str(log_lines), self.container_name], capture_output=True, text=True)
        return f"{state.stdout.strip() or state.stderr.strip()}\n--- last {log_lines} log lines ---\n{logs.stdout}{logs.stderr}"

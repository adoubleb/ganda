"""An engine's connection settings from the `.env` file in its provider directory.

`ganda <engine> ...` reads `ganda/<engine>/.env` (next to `ganda/<engine>/.env.example`,
which lists the settings) before the engine's module is imported, since some settings
are read at import. The file is a list of `KEY=value` lines; `#` starts a comment, an
`export ` prefix and one pair of quotes are accepted.

- A variable already in the environment wins over the file, so a CI job or an exported
  shell variable is never overwritten.
- A key with an empty value is the same as a key left out. That is how the file selects
  Docker: leave `ORIENT_URL` / `ARANGO_URL` unset and the provider starts a container;
  set it and the provider attaches to that server.
"""
from __future__ import annotations

import os
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2]


def parse(text: str) -> dict:
    """The `KEY=value` pairs of a file's text, empty values left out."""
    out: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if value:
            out[key] = value
    return out


def find(engine: str, package: "Path | None" = None) -> "Path | None":
    path = (package or PACKAGE) / engine / ".env"
    return path if path.is_file() else None


def load(engine: str, package: "Path | None" = None) -> "Path | None":
    """Put `ganda/<engine>/.env`'s settings into `os.environ` (never over one already there).
    The file read, or None when there is none."""
    path = find(engine, package)
    if path is None:
        return None
    for key, value in parse(path.read_text()).items():
        os.environ.setdefault(key, value)
    return path

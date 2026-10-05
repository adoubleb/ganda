"""The engines ganda knows: a name on the command line -> its provider.

A provider is imported only when it is asked for, so one engine's dependencies never
load for another's campaign. To add an engine, add its `module:attribute` here.
"""
from __future__ import annotations

import importlib

PROVIDERS = {
    "orientdb": "ganda.orientdb.provider:PROVIDER",
    "surrealdb": "ganda.surrealdb.provider:PROVIDER",
    "arangodb": "ganda.arangodb.provider:PROVIDER",
}


def provider(name: str):
    try:
        module, attribute = PROVIDERS[name].split(":")
    except KeyError:
        raise SystemExit(f"unknown engine {name!r}; known: {', '.join(PROVIDERS)}") from None
    return getattr(importlib.import_module(module), attribute)

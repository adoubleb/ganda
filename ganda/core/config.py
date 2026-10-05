"""The campaign configuration as command-line flags: every field of a provider's
configuration class is a flag (`--max-node-types`, `--collate-probability`, ...), so a
bundle's `repro.py` and a mutation report carry the exact configuration that ran."""
from __future__ import annotations

import argparse
import dataclasses

from ganda.core.data_generator import GeneratorConfig

__all__ = ["GeneratorConfig", "add_config_arguments", "config_from_args"]


def _bool(text: str) -> bool:
    return text.lower() in ("1", "true", "yes")


def add_config_arguments(ap: argparse.ArgumentParser, config_class) -> None:
    for f in dataclasses.fields(config_class):
        flag = "--" + f.name.replace("_", "-")
        ap.add_argument(flag, type=_bool if isinstance(f.default, bool) else type(f.default), default=f.default)


def config_from_args(config_class, args):
    names = {f.name for f in dataclasses.fields(config_class)}
    return config_class(**{k: v for k, v in vars(args).items() if k in names})

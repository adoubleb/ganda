"""The SAP HANA findings already filed, each withheld at its exact shape per variant so a
campaign does not report it again. None is filed yet: a triaged finding gets an id
(`H1`, `H2`, ...), a write-up under `issues/`, and a guard here -- a function of the
case's AST and the variant's concrete syntax that answers its id where the variant's
text is the bug's shape (`ganda/orientdb/known_bugs.py` is the pattern)."""
from __future__ import annotations


def mark_known_bugs(case, q, schema, instance):
    """A traversal case with the variants that meet a filed bug marked."""
    return case


def select_known(q, syntax, rewrites, data_model, schema, instance):
    return None


def write_known(q, syntax, rewrites, data_model, schema, instance):
    return None

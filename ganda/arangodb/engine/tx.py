"""The transaction door's text: `TX_MARK`, the collections as JSON, then the statements
separated by `SEPARATOR`, the question last; the session aborts it."""
from __future__ import annotations

import json

TX_MARK = "/* ganda:tx */"
SEPARATOR = "\n;;\n"


def script(collections: dict, statements: list, question: str) -> str:
    """`collections`: `{"write": [...], "read": [...]}` as `/_api/transaction/begin` takes them."""
    return TX_MARK + " " + json.dumps(collections, sort_keys=True) + SEPARATOR + SEPARATOR.join(statements + [question])


def is_script(text: str) -> bool:
    return text.startswith(TX_MARK)


def parts(text: str) -> "tuple[dict, list, str]":
    """(collections, statements, question) of a door text."""
    head, *rest = text[len(TX_MARK):].strip().split(SEPARATOR)
    return json.loads(head), rest[:-1], rest[-1]

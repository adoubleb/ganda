from ganda.orientdb.materialize.document import build_document_setup
from ganda.orientdb.materialize.graph import build_graph_setup
from ganda.orientdb.materialize.relational import build_relational_setup

BUILDERS = {"doc": build_document_setup, "graph": build_graph_setup, "rel": build_relational_setup}

__all__ = ["BUILDERS", "build_document_setup", "build_graph_setup", "build_relational_setup"]

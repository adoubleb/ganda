from ganda.arangodb.mapping.document import build_document_setup
from ganda.arangodb.mapping.graph import build_graph_setup
from ganda.arangodb.mapping.relational import build_relational_setup

MAPPERS = {"doc": build_document_setup, "graph": build_graph_setup, "rel": build_relational_setup}

__all__ = ["MAPPERS", "build_document_setup", "build_graph_setup", "build_relational_setup"]

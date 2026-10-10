from ganda.hana.mapping.document import build_document_setup
from ganda.hana.mapping.graph import build_graph_setup
from ganda.hana.mapping.rdf import build_rdf_setup
from ganda.hana.mapping.relational import build_relational_setup

MAPPERS = {"doc": build_document_setup, "rel": build_relational_setup, "graph": build_graph_setup, "rdf": build_rdf_setup}

__all__ = ["MAPPERS", "build_document_setup", "build_graph_setup", "build_rdf_setup", "build_relational_setup"]

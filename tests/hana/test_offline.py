"""The offline SAP HANA stand-in (`offline.py`: SQLite for rel and doc, rdflib for rdf).
Its whole campaigns are not run here (minutes each, rdflib's SPARQL): `python3 -m
tests.hana.run_offline --seeds 1 20` runs them through the provider's own session code
and prints what disagrees. Needs rdflib (`pip install rdflib`)."""
from __future__ import annotations

import pytest

rdflib = pytest.importorskip("rdflib")


def test_rdflib_quirks_the_offline_runner_works_around():
    """What rdflib answers against the SPARQL 1.1 standard (if one of these starts
    failing, rdflib was fixed: drop the workaround it names in `run_offline.py` /
    `offline.py`)."""
    p = "PREFIX c: <urn:c:> PREFIX p: <urn:p:> "
    ds = rdflib.Dataset()
    ds.update(p + "INSERT DATA { GRAPH <urn:g> { <urn:a> a c:t ; p:id 'a' ; p:f 1 . <urn:b> a c:t ; p:id 'b' ; p:f 2 . <urn:c> a c:t ; p:id 'c' ; p:f 3 . } }")

    def ids(q):
        return sorted(tuple(str(x) for x in r) for r in ds.query(p + q))

    # `error && false` is false in the standard: rdflib drops the row (the `native` variants)
    assert ids("SELECT ?id WHERE { GRAPH <urn:g> { ?n p:id ?id . OPTIONAL { ?n p:nope ?u } FILTER (!((?u = 1) && false)) } }") == []
    # `IN` is a disjunction of `=` in the standard (numeric promotion: HANA, probe K24);
    # rdflib compares IN by term, so a long against a double never matches, while its
    # `=` promotes -- the SPARQL literal of an integer constant on a FLOAT field is a double
    # (`predicate._lit`)
    ds.update(p + "INSERT DATA { GRAPH <urn:g> { <urn:d> a c:t ; p:id 'd' ; p:f '2.0'^^<http://www.w3.org/2001/XMLSchema#double> } }")
    assert ids("SELECT ?id WHERE { GRAPH <urn:g> { ?n p:id ?id ; p:f ?f FILTER (?f = 2) } }") == [("b",), ("d",)]
    assert ids("SELECT ?id WHERE { GRAPH <urn:g> { ?n p:id ?id ; p:f ?f FILTER (?f IN (2)) } }") == [("b",)]
    ds.update(p + "DELETE WHERE { GRAPH <urn:g> { <urn:d> ?x ?y } }")
    # an independent FILTER EXISTS changes a join with a subquery (the `jscan` variants)
    q = "SELECT ?id ?v WHERE { GRAPH <urn:g> { ?n a c:t ; p:id ?id . %s { ?n p:f ?f . { SELECT ?m ?mf WHERE { ?m a c:t ; p:f ?mf } } FILTER (?mf > ?f) } ?m p:id ?v . } }"
    assert len(ids(q % "")) == 3 and len(ids(q % "FILTER (EXISTS { ?n p:f ?z })")) == 2

"""Campaigns of the SAP HANA provider against the offline stand-in (`offline.py`: SQLite
for rel and doc, rdflib for rdf), through the provider's own session code: every case
must agree pair by pair and with the reference, every setup and write must leave the
Instance's state. Needs rdflib (`pip install rdflib`); `python3 -m
tests.hana.run_offline --seeds 1 20` runs more seeds and prints what disagrees."""
from __future__ import annotations

import pytest

rdflib = pytest.importorskip("rdflib")

from tests.hana.run_offline import run_seed  # noqa: E402
from ganda.hana.provider import PROVIDER as E  # noqa: E402

CFG = E.config_class(write_form_probability=0.8, churn_edge_update_probability=0.3, clusters_probability=0.3)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_an_offline_campaign_agrees_with_itself_and_the_reference(seed):
    r = run_seed(seed, CFG)
    assert not r["state"], r["state"]
    assert not r.get("write_diffs"), (r.get("write_stopped"), r.get("write_diffs"))
    bad = [(x.finding_class, x.case.generator_name, x.case.shape, x.failing_pairs[:4]) for x in r["results"] if not x.ok]
    assert not bad, bad[:5]
    assert len(r["results"]) > 100


@pytest.mark.parametrize("seed", [1, 2])
def test_an_offline_campaign_with_the_base_class_and_custom_keys(seed):
    """The inheritance axis (a base class on four data models: a UNION ALL view, the
    views' union, a second class triple, the unlabelled MATCH; writes on it per
    subclass) and custom keys, through every variant and every write."""
    cfg = CFG.with_(inheritance_probability=1.0, custom_key_probability=1.0)
    r = run_seed(seed, cfg)
    assert not r["state"], r["state"]
    assert not r.get("write_diffs"), (r.get("write_stopped"), r.get("write_diffs"))
    bad = [(x.finding_class, x.case.generator_name, x.case.shape, x.failing_pairs[:4]) for x in r["results"] if not x.ok]
    assert not bad, bad[:5]
    assert any(x.case.params.get("table") == "N0" for x in r["results"])


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
    # `=` promotes -- the SPARQL literal of an integer pivot on a FLOAT field is a double
    # (`predicate._lit`)
    ds.update(p + "INSERT DATA { GRAPH <urn:g> { <urn:d> a c:t ; p:id 'd' ; p:f '2.0'^^<http://www.w3.org/2001/XMLSchema#double> } }")
    assert ids("SELECT ?id WHERE { GRAPH <urn:g> { ?n p:id ?id ; p:f ?f FILTER (?f = 2) } }") == [("b",), ("d",)]
    assert ids("SELECT ?id WHERE { GRAPH <urn:g> { ?n p:id ?id ; p:f ?f FILTER (?f IN (2)) } }") == [("b",)]
    ds.update(p + "DELETE WHERE { GRAPH <urn:g> { <urn:d> ?x ?y } }")
    # an independent FILTER EXISTS changes a join with a subquery (the `jscan` variants)
    q = "SELECT ?id ?v WHERE { GRAPH <urn:g> { ?n a c:t ; p:id ?id . %s { ?n p:f ?f . { SELECT ?m ?mf WHERE { ?m a c:t ; p:f ?mf } } FILTER (?mf > ?f) } ?m p:id ?v . } }"
    assert len(ids(q % "")) == 3 and len(ids(q % "FILTER (EXISTS { ?n p:f ?z })")) == 2

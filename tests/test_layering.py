"""The layering rule: the core knows no engine, and every registered provider fills the
contract."""
import ast
import re
import importlib
import pathlib

from ganda.core.data.limits import StorageLimits
from ganda.core.engine.provider import Provider, Semantics
from ganda.registry import PROVIDERS, provider

ROOT = pathlib.Path(__file__).resolve().parent.parent / "ganda"
ENGINE_PACKAGES = tuple(f"ganda.{name}" for name in PROVIDERS)


def _imports(path: pathlib.Path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def test_the_core_imports_no_provider():
    offenders = [(str(p.relative_to(ROOT)), m) for p in sorted((ROOT / "core").rglob("*.py")) for m in _imports(p) if m.startswith(ENGINE_PACKAGES)]
    assert not offenders, offenders


def test_the_core_imports_no_registry_at_module_level():
    # `replay` looks the engine up by name when it is called; nothing else may
    for p in sorted((ROOT / "core").rglob("*.py")):
        tree = ast.parse(p.read_text())
        top = [n.module for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "ganda.registry"]
        assert not top, p


# engine names and schema vocabulary the core must not use, in code or comments: an
# engine option is a value the provider draws and reads (`IndexSpec.options`,
# `NodeTypeSpec.options`, `IndexSpec.kind`, `Schema.id_index`)
ENGINE_WORDS = (
    "OrientDB", "ArangoDB", "HANA", "SurrealDB", "AQL", "SPARQL", "Cypher", "RID",
    "NOTUNIQUE", "DICTIONARY", "HASH_INDEX", "CLUSTERS", "BY KEY", "BY VALUE",
    "EMBEDDED", "EMBEDDEDMAP", "COLLATE", "EXTENDS V",
    # OrientDB's operator spellings: the core names them `HAS`, `HAS ANY`, `HAS ALL`,
    # `HAS KEY`, `HAS VALUE`, `VALUES HAS`, `IS PRESENT`, `IS ABSENT`
    "CONTAINS", "CONTAINSANY", "CONTAINSALL", "CONTAINSKEY", "CONTAINSVALUE",
    "IS DEFINED", "IS NOT DEFINED",
)


def test_the_core_names_no_engine():
    pattern = re.compile(r"(?<![A-Za-z_])(" + "|".join(re.escape(w) for w in ENGINE_WORDS) + r")(?![A-Za-z_])")
    offenders = [f"{p.relative_to(ROOT)}:{i}: {m.group(1)}" for p in sorted((ROOT / "core").rglob("*.py")) for i, line in enumerate(p.read_text().splitlines(), 1) for m in pattern.finditer(line)]
    assert not offenders, offenders


def test_providers_do_not_import_each_other():
    for name in PROVIDERS:
        others = tuple(f"ganda.{n}" for n in PROVIDERS if n != name)
        offenders = [(str(p.relative_to(ROOT)), m) for p in sorted((ROOT / name).rglob("*.py")) for m in _imports(p) if m.startswith(others)]
        assert not offenders, offenders


def test_every_registered_provider_loads_and_names_itself():
    for name in PROVIDERS:
        engine = provider(name)
        assert isinstance(engine, Provider) and engine.name == name and engine.data_models
        assert engine.config_class is not None
        assert isinstance(engine.limits, StorageLimits), f"{name} declares no limits"
        assert callable(engine.server) and engine.database_name("ns", engine.data_models[0])


def test_a_full_provider_fills_every_slot():
    for name in PROVIDERS:
        engine = provider(name)
        if not engine.has_generators:
            continue
        assert isinstance(engine.semantics, Semantics)
        assert set(engine.mappers) == set(engine.data_models)
        for slot, members in {
            "pivots": ("NULL_OPS", "ORDERED_OPS", "LIKE_WILDCARDS", "ordered_ops", "cross_type", "refine"),
            "physical_design": ("draw_index_kind", "NON_UNIQUE_KINDS", "EDGE_INDEX_DATA_MODELS", "is_unique", "holds_one_record_per_key", "demoted_kind", "index_key_value", "draw_id_indexes", "draw_index_options", "draw_storage_options", "draw_write_form", "is_valid_key", "BOUNDARY_VALUES"),
            "traversal_syntax": ("syntaxes_for", "render", "can_pull_up"),
            "select_syntax": ("BASE", "METHODS", "method_fn", "build_rewrites", "extra_binds", "rewrite_params", "base_data_models", "syntaxes_for", "applicable", "render"),
            "path_syntax": ("render_all", "bind_vars"),
            "write_syntax": ("build_rewrites", "all_syntaxes", "applicable", "render", "execute", "shared_binds", "rewrite_params"),
            "rewrites": ("arith_exact",),
            "known_bugs": ("mark_known_bugs", "select_known", "write_known"),
        }.items():
            missing = [m for m in members if not hasattr(getattr(engine, slot), m)]
            assert not missing, (name, slot, missing)
        assert engine.mutants and len({m.name for m in engine.mutants}) == len(engine.mutants)
        # the oracle's two provider-set options (`core/engine/provider.py`)
        assert isinstance(engine.compare_despite_errors, bool) and isinstance(engine.partial_traversals, bool) and isinstance(engine.error_signature("[1] x"), str)


def test_the_fake_engine_fills_the_same_contract():
    fake = importlib.import_module("tests.fake_engine").FakeProvider()
    assert isinstance(fake, Provider) and isinstance(fake.semantics, Semantics) and isinstance(fake.limits, StorageLimits)



def test_every_providers_setup_steps_load_side_by_side():
    # the step registry is shared: two providers' plans must both round-trip in one process
    from ganda.core.engine.setup_plan import plan_from_dict, plan_to_dict

    for name in PROVIDERS:
        engine = provider(name)
        if engine.has_generators:
            assert engine.mappers
    from ganda.orientdb.storage.mapping.common import Sql as OrientSql

    assert type(plan_from_dict(plan_to_dict([OrientSql("SELECT 1")]))[0]) is OrientSql

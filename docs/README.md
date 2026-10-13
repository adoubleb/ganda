# Ganda developer docs

Start here if you are new to the code.

1. [architecture.md](architecture.md): what Ganda does, the core/provider split, one
   seed end to end, the provider contract, and the invariants that make a finding
   trustworthy.
2. [contributing.md](contributing.md): setup, the repository map, the gates every change
   passes, the rules, and step-by-step guides (add a concrete syntax, a semantics rule, a
   generation axis, a known bug, a provider; triage a finding).
3. [glossary.md](glossary.md): every term, with the file it lives in.

Engine-specific detail lives next to the code: `ganda/<engine>/RULES.md` lists the
engine facts each provider relies on, `ganda/hana/HANDOFF.md` is the guide to live HANA
work, and `ganda/surrealdb/PORTING.md` maps a legacy fuzzer onto the provider slots.

A good first afternoon:

```bash
pip install -e ".[dev]"
pytest tests/ -q
python3 -m ganda orientdb run --iterations 3 --seed 1     # needs Docker
```

Then read `ganda/core/campaign.py::run_instance`, `tests/fake_engine/__init__.py` and one
bundle under `bugs/`.

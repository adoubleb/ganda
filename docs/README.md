# Ganda developer docs

Start here if you are new to the code.

1. [architecture.md](architecture.md)
2. [contributing.md](contributing.md): setup, the repository map, and step-by-step guides (add a concrete syntax, a semantics rule, a
   generation axis, a known bug, a provider; triage a finding).
3. [glossary.md](glossary.md): every term, with the file it lives in.


```bash
pip install -e ".[dev]"
pytest tests/ -q
python3 -m ganda orientdb run --iterations 3 --seed 1     # needs Docker
```

Then read `ganda/core/campaign/run.py::run_instance`, `tests/fake_engine/__init__.py` and one
bundle under `bugs/`.

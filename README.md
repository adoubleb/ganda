# What is Ganda?

A multi-model metamorphic fuzzer for DBMSs that can find logic bugs (but also finds crashes/hangs/etc.)

# How does Ganda work?
On a high level, each fuzzing round randomly generates a Python object representing a property-graph.

Using user-supplied translators, each model under test materializes the object in the DBMS as a single database. 

For each fuzzing round, multiple query ASTs are generated from four query IR generators. DBMS specific provider classes provide translator methods to translate the ASTs into concrete queries.

Ganda then runs the queries on the DBMS under test and results are normalized. Normalized results are compared and any difference between normalized results suggests a bug. The query and the setup is then logged for a human (or LLM ◡̈) review.


New contributors: start with [`docs/`](docs/README.md) (architecture, contributing guide, glossary).

## Running

GANDA defaults to using the providers latest docker image for fuzzing. To use a running database, set the environment
variables in /<engine>/.env

```bash
pip install -e ".[dev]"          # or run from this directory with python3 -m ganda

ganda list                                             # list the engines GANDA supports
pytest tests/ -q                                       # test that GANDA is running correctly

## Before fuzzing OrientDB with GANDA
ganda orientdb --help # to see list of all commands
ganda orientdb doctor # to check if dbms and all supported models are running
ganda orientdb probe # to check if orientdb properties Ganda relies on still holds

# To run fuzzing campaigns
ganda orientdb run --iterations 100 --seed 1000 --workers 2 
ganda orientdb run --iterations 20 --seed 1 --no-known-bugs      # re-confirm the filed bugs by dropping known bug guards
python3 scripts/campaign.py --tag c100 --iterations 100          # best for long running campaign
python3 bugs/<bundle>/repro.py                         # replay one finding
```

## Scale: the storage-engine stress mode

```bash
ganda orientdb limits                                  # what the engine can hold, and the large profile on it
ganda orientdb run --scale large --iterations 5 --seed 1 --run-tag large
ganda orientdb run --scale large --max-nodes-per-type 20000 --large-row-probability 0.01
python3 scripts/campaign.py --tag l50 --iterations 50 --scale large
```

`--scale large` stresses each engine's storage layer with volume and size: classes of thousands of rows, relationship types of thousands of edges with high fan-out hubs, and strings, binaries and lists grown toward the engine's limits.

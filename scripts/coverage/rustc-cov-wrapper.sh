#!/bin/bash
# RUSTC_WRAPPER: LLVM source-based coverage (continuous mode on Linux needs counter
# relocation) for SurrealDB's own crates only; dependencies build as usual.
rustc="$1"; shift
name=""
prev=""
for a in "$@"; do
  if [ "$prev" = "--crate-name" ]; then name="$a"; fi
  prev="$a"
done
case "$name" in
  surreal|surrealdb|surrealdb_*) exec "$rustc" "$@" -C instrument-coverage -C llvm-args=-runtime-counter-relocation ;;
  *) exec "$rustc" "$@" ;;
esac

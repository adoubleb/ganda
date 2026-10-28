#!/bin/bash
set -euxo pipefail
apt-get update -qq && apt-get install -y -qq cmake clang libclang-dev protobuf-compiler >/dev/null
rustup component add llvm-tools
cd /src
export RUSTC_WRAPPER=/build/rustc-cov-wrapper.sh
export CARGO_TARGET_DIR=/build/target
export CARGO_PROFILE_RELEASE_LTO=false CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16 CARGO_PROFILE_RELEASE_STRIP=none
# instrument-coverage links the profiler runtime only where rustc instruments the final crate (surreal)
cargo build --release --locked --bin surreal -j 48
ls -la /build/target/release/surreal

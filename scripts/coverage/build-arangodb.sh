#!/bin/bash
# ArangoDB 3.12.11 (community) with clang source-based coverage, continuous mode:
# only arangod/ and lib/ are instrumented (-fprofile-list).
set -euxo pipefail
cat > /build/profile-list.txt <<LIST
source:/src/arangod/*
source:/src/lib/*
LIST
COV="-fprofile-instr-generate -fcoverage-mapping -mllvm -runtime-counter-relocation -fprofile-list=/build/profile-list.txt"
cmake --preset pr-non-maintainer-x64 -G Ninja -S /src -B /build/out \
  -DUSE_ENTERPRISE=Off \
  -DCMAKE_C_COMPILER=/tools/clang -DCMAKE_CXX_COMPILER=/tools/clang++ \
  -DOPENSSL_ROOT_DIR=/opt -DCMAKE_LIBRARY_PATH=/opt/lib \
  -DUSE_V8=On -DUSE_MINIMAL_DEBUGINFO=On -DCMAKE_INSTALL_PREFIX=/ \
  "-DCMAKE_C_FLAGS=-fno-stack-protector $COV" \
  "-DCMAKE_CXX_FLAGS=-fno-stack-protector $COV" \
  "-DCMAKE_EXE_LINKER_FLAGS=-Wl,--build-id=sha1 -fuse-ld=lld -fprofile-instr-generate"
cmake --build /build/out --parallel 48 --target arangod
ls -la /build/out/bin/arangod

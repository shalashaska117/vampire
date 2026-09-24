#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
profile=${1:-coverage}
jobs=${JOBS:-4}
build="build/testing/$profile"
args=(-S . -B "$build")
if test -n "${Z3_DIR:-}"; then args+=("-DZ3_DIR=$Z3_DIR"); fi
case "$profile" in
  coverage)
    args+=(-DCMAKE_BUILD_TYPE=Debug -DCHECK_LEAKS=ON
      '-DCMAKE_CXX_FLAGS=--coverage -fprofile-update=atomic'
      -DCMAKE_EXE_LINKER_FLAGS=--coverage)
    targets=(vampire vtest) ;;
  debug|memcheck)
    args+=(-DCMAKE_BUILD_TYPE=Debug -DCHECK_LEAKS=ON)
    targets=(vampire vtest) ;;
  ubsan)
    args+=(-DCMAKE_BUILD_TYPE=Debug -DCHECK_LEAKS=ON -DUBSAN=ON
      -DCMAKE_CXX_FLAGS=-fno-sanitize-recover=undefined)
    targets=(vampire vtest) ;;
  asan)
    args+=(-DCMAKE_BUILD_TYPE=Debug -DCHECK_LEAKS=ON
      '-DCMAKE_CXX_FLAGS=-fsanitize=address -fno-omit-frame-pointer'
      -DCMAKE_EXE_LINKER_FLAGS=-fsanitize=address)
    targets=(vampire vtest) ;;
  release)
    args+=(-DCMAKE_BUILD_TYPE=Release)
    targets=(vampire) ;;
  no-z3)
    args+=(-DCMAKE_BUILD_TYPE=Debug -DCMAKE_DISABLE_FIND_PACKAGE_Z3=ON)
    targets=(vampire vtest) ;;
  *) echo "unknown profile: $profile" >&2; exit 2 ;;
esac
cmake "${args[@]}"
cmake --build "$build" --target "${targets[@]}" -j "$jobs"

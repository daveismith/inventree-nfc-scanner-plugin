#!/bin/sh
# Build the firmware's simulator (host_sim) for the firmware-in-the-loop tests, from a checkout of
# the firmware repository, in Espressif's IDF image, for this machine's CPU. The checkout is
# only read; the build happens in the container. The binary lands where sim_scanner.py looks:
#
#   tests/browser/sim.sh [--firmware ../inventree_nfc_scanner]   ->   tests/browser/.sim/host_sim
set -eu

here=$(cd "$(dirname "$0")" && pwd)
firmware=$(cd "$here/../../.." && pwd)/inventree_nfc_scanner
[ "${1:-}" = --firmware ] && firmware=$(cd "$2" && pwd)
[ -f "$firmware/host_sim/CMakeLists.txt" ] || { echo "no firmware checkout at $firmware (--firmware DIR)" >&2; exit 2; }
mkdir -p "$here/.sim"
docker run --rm -v "$firmware:/src:ro" -v "$here/.sim:/out" mirror.gcr.io/espressif/idf:v6.1 bash -c '
    set -e
    . "$IDF_PATH/export.sh" > /dev/null 2>&1
    mkdir /build
    tar -C /src --exclude=./.git --exclude=./build --exclude=./build-dev --exclude=./dist \
        --exclude=./host_sim/build --exclude=./host_test/build -cf - . | tar -x -C /build
    cd /build/host_sim && idf.py --preview set-target linux > /dev/null && idf.py build > /dev/null
    install -m 755 build/host_sim.elf /out/host_sim
'
echo "$here/.sim/host_sim: the firmware's simulator, version $(cat "$firmware/version.txt")"

#!/bin/sh
# Run the test suite against one InvenTree version, in a container with no network.
#
#   tests/run.sh [--version 1.4.3] [--no-build] [pytest arguments...]
#
# Builds inventree-nfc-test:<version> (tests/Dockerfile) unless --no-build, mounts this working
# tree at /plugin, installs the plugin from it and runs pytest. With no pytest arguments it runs
# the pure and server layers.
set -eu

here=$(cd "$(dirname "$0")/.." && pwd)
version=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["supported"][0])' "$here/tests/inventree-versions.json")
build=1
while [ $# -gt 0 ]; do
    case "$1" in
        --version) version=$2; shift 2 ;;
        --no-build) build=0; shift ;;
        *) break ;;
    esac
done
[ $# -gt 0 ] || set -- -m "pure or server"

image="inventree-nfc-test:$version"
if [ "$build" = 1 ]; then
    docker build -q -f "$here/tests/Dockerfile" --build-arg "INVENTREE_VERSION=$version" -t "$image" "$here/tests" >/dev/null
fi

tty=
[ -t 1 ] && tty=-t
exec docker run --rm $tty --network none \
    -v "$here:/plugin" \
    -e "INVENTREE_TEST_VERSION=$version" \
    -e GITHUB_ACTIONS \
    "$image" sh -c 'pip install -q --no-deps --no-build-isolation --no-index -e . 2>&1 | grep -v "^$" >&2 || true; exec python -m pytest "$@"' pytest "$@"

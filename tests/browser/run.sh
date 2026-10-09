#!/bin/sh
# Run the browser layer against one InvenTree version: the stack in compose.yaml comes up on
# an internal network, the tests run in its `tests` container, and it all goes away after.
#
#   tests/browser/run.sh [--version 1.4.3] [--no-build] [pytest arguments...]
#
# --no-build uses the inventree-nfc-browser-tests image as it is (CI builds it with a cache).
# Results (JUnit XML, and for a failed test its trace, screenshot and GIF) go to
# test-results/; the stack's logs to test-results/browser-stack-<version>.log.
set -eu

here=$(cd "$(dirname "$0")/../.." && pwd)
version=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["supported"][0])' "$here/tests/inventree-versions.json")
build=1
while [ $# -gt 0 ]; do
    case "$1" in
        --version) version=$2; shift 2 ;;
        --no-build) build=0; shift ;;
        *) break ;;
    esac
done
# The browser tests, unless a path among the arguments says which.
case " $* " in *" tests/"*) ;; *) set -- "$@" tests/browser ;; esac
export INVENTREE_VERSION="$version"

mkdir -p "$here/test-results"
compose="docker compose -p nfc-browser-$(echo "$version" | tr -c 'a-z0-9\n' '-')-$$ -f $here/tests/browser/compose.yaml"
cleanup() {
    $compose logs --no-color > "$here/test-results/browser-stack-$version.log" 2>&1 || true
    $compose down -v --remove-orphans > /dev/null 2>&1 || true
}
trap cleanup EXIT

[ "$build" = 0 ] || $compose build --quiet tests
$compose pull --quiet --ignore-buildable 2>/dev/null || true
$compose run --rm tests python -m pytest -m browser \
    --output test-results/browser --video retain-on-failure --screenshot only-on-failure \
    --tracing retain-on-failure "$@"

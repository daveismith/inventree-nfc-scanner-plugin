#!/bin/sh
# Run the browser layer against one InvenTree version: the stack in compose.yaml comes up on
# an internal network, the tests run in its `tests` container, and it all goes away after.
#
#   tests/browser/run.sh [--version 1.4.3] [--no-build] [--firmware] [pytest arguments...]
#
# --firmware runs the tests against the firmware's own code (marked `firmware`; the simulator
# from tests/browser/sim.sh, which must be there) instead of the rest.
#
# The tests run in the image tests/browser/image.sh names: used as it is if it is here already,
# else pulled from GitHub's registry, else (a change to tests.Dockerfile or requirements.txt not
# yet built there, or no access) built here. --no-build uses the local image as it is.
# Results (JUnit XML, and for a failed test its trace, screenshot and GIF) go to
# test-results/; the stack's logs to test-results/browser-stack-<version>.log.
set -eu

here=$(cd "$(dirname "$0")/../.." && pwd)
version=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["supported"][0])' "$here/tests/inventree-versions.json")
build=1
marks="browser and not firmware"
while [ $# -gt 0 ]; do
    case "$1" in
        --version) version=$2; shift 2 ;;
        --no-build) build=0; shift ;;
        --firmware) marks=firmware; export REQUIRE_SIM=1; shift ;;
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

local_image=inventree-nfc-browser-tests  # the name compose.yaml runs
if [ "$build" = 1 ]; then
    image=$("$here/tests/browser/image.sh")
    hash=$("$here/tests/browser/image.sh" --hash)
    have=$(docker image inspect --format '{{ index .Config.Labels "nfc.browser-tests.hash" }}' "$local_image" 2>/dev/null || true)
    if [ "$have" != "$hash" ]; then
        if docker pull --quiet "$image" >/dev/null 2>&1 || { sleep 10; docker pull --quiet "$image" >/dev/null 2>&1; }; then
            docker tag "$image" "$local_image"
        else
            echo "building the test image ($image is not in the registry, or not reachable from here)" >&2
            docker build --quiet --label "nfc.browser-tests.hash=$hash" -f "$here/tests/browser/tests.Dockerfile" \
                -t "$local_image" "$here/tests/browser" >/dev/null
        fi
    fi
fi
# What is not here yet; an image already pulled is used as it is.
$compose pull --quiet --ignore-buildable --policy missing 2>/dev/null || true
$compose run --rm tests python -m pytest -m "$marks" \
    --output test-results/browser --video retain-on-failure --screenshot only-on-failure \
    --tracing retain-on-failure "$@"

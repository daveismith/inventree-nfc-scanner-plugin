#!/bin/sh
# Run the test suite against one InvenTree version, in a container with no way out.
#
#   tests/run.sh [--version 1.4.3] [--db sqlite|postgres] [--no-build] [pytest arguments...]
#
# Builds inventree-nfc-test:<version> (tests/Dockerfile) unless --no-build, mounts this working
# tree at /plugin, installs the plugin from it and runs pytest. With no pytest arguments it runs
# the pure and server layers.
#
# SQLite: the container has no network at all. PostgreSQL: the database and the tests share a
# Docker network of their own, created `--internal` (no route off the host, nothing published)
# and removed afterwards.
#
# Results (JUnit XML, coverage data) go to test-results/ in the working tree.
set -eu

here=$(cd "$(dirname "$0")/.." && pwd)
version=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["supported"][0])' "$here/tests/inventree-versions.json")
db=sqlite
build=1
while [ $# -gt 0 ]; do
    case "$1" in
        --version) version=$2; shift 2 ;;
        --db) db=$2; shift 2 ;;
        --no-build) build=0; shift ;;
        *) break ;;
    esac
done
[ $# -gt 0 ] || set -- -m "pure or server"

image="inventree-nfc-test:$version"
if [ "$build" = 1 ]; then
    docker build -q -f "$here/tests/Dockerfile" --build-arg "INVENTREE_VERSION=$version" -t "$image" "$here/tests" >/dev/null
fi
mkdir -p "$here/test-results"

net="--network none"
dbenv=""
if [ "$db" = postgres ]; then
    name="nfc-test-$$"
    docker network create --internal "$name" >/dev/null
    trap 'docker rm -f "$name-db" >/dev/null 2>&1; docker network rm "$name" >/dev/null 2>&1' EXIT
    docker run -d --name "$name-db" --network "$name" --network-alias db \
        -e POSTGRES_USER=inventree -e POSTGRES_PASSWORD=inventree -e POSTGRES_DB=inventree \
        postgres:17 >/dev/null
    i=0
    until docker exec "$name-db" pg_isready -q -U inventree; do
        i=$((i + 1)); [ $i -lt 60 ] || { echo "PostgreSQL did not start" >&2; exit 1; }
        sleep 1
    done
    net="--network $name"
    dbenv="-e INVENTREE_DB_ENGINE=postgresql -e INVENTREE_DB_HOST=db -e INVENTREE_DB_PORT=5432
           -e INVENTREE_DB_NAME=inventree -e INVENTREE_DB_USER=inventree -e INVENTREE_DB_PASSWORD=inventree"
elif [ "$db" != sqlite ]; then
    echo "--db is sqlite or postgres" >&2
    exit 2
fi

tty=
[ -t 1 ] && tty=-t
# Not exec: the trap above removes the database afterwards.
# shellcheck disable=SC2086 # $net and $dbenv are lists of arguments
docker run --rm $tty $net $dbenv \
    -v "$here:/plugin" \
    -e "INVENTREE_TEST_VERSION=$version" -e "INVENTREE_TEST_DB=$db" \
    -e GITHUB_ACTIONS -e COVERAGE_FILE \
    "$image" sh -c 'pip install -q --no-deps --no-build-isolation --no-index -e . 2>&1 | grep -v "^$\|as the .root. user" >&2 || true; exec python -m pytest "$@"' pytest "$@"

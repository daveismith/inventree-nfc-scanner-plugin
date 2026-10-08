#!/bin/sh
# One-time (and idempotent) setup of the local InvenTree after `docker compose up -d`:
# the plugin integrations InvenTree needs, the base URL, the plugin's activation, and a
# restart so that its database tables and static files are in place. See README.md.
set -e
cd "$(dirname "$0")"

# .env is compose's, not the shell's: values with spaces, $ or # would break `. ./.env`.
env_value() { grep "^$1=" .env | head -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"; }
INVENTREE_HTTP_PORT="$(env_value INVENTREE_HTTP_PORT)"
INVENTREE_SITE_URL="$(env_value INVENTREE_SITE_URL)"
INVENTREE_ADMIN_USER="$(env_value INVENTREE_ADMIN_USER)"
INVENTREE_ADMIN_PASSWORD="$(env_value INVENTREE_ADMIN_PASSWORD)"
[ -n "$INVENTREE_SITE_URL" ] && [ -n "$INVENTREE_ADMIN_USER" ] || { echo "dev/.env is missing or incomplete; see README.md"; exit 1; }
BASE="http://127.0.0.1:${INVENTREE_HTTP_PORT:-8080}"
HOST="Host: ${INVENTREE_SITE_URL#http://}"
AUTH="$INVENTREE_ADMIN_USER:$INVENTREE_ADMIN_PASSWORD"

say() { printf '%s\n' "$*"; }
fail() { say "$*"; exit 1; }

# A PATCH or GET that must succeed: -f makes a 4xx or 5xx an error, which set -e stops on.
api() { curl -sf -o /dev/null -u "$AUTH" -H "$HOST" -H 'Content-Type: application/json' "$@"; }

wait_for() {
    # $1: what we wait for (for the message); the rest: a curl command that must succeed
    what="$1"; shift
    for i in $(seq 1 60); do
        if "$@" > /dev/null 2>&1; then return 0; fi
        sleep 5
    done
    fail "gave up waiting for $what; see: docker compose logs inventree-server"
}

say "waiting for InvenTree at $INVENTREE_SITE_URL ..."
wait_for "the API" curl -sf -o /dev/null -H "$HOST" "$BASE/api/"

say "enabling plugin integrations"
for key in ENABLE_PLUGINS_URL ENABLE_PLUGINS_APP ENABLE_PLUGINS_INTERFACE ENABLE_PLUGINS_EVENTS ENABLE_PLUGINS_SCHEDULE; do
    api -X PATCH "$BASE/api/settings/global/$key/" -d '{"value":"True"}' || fail "could not set $key"
done
api -X PATCH "$BASE/api/settings/global/INVENTREE_BASE_URL/" -d "{\"value\":\"$INVENTREE_SITE_URL\"}" || fail "could not set the base URL"

say "an admin API token, in admin.token"
token="$(curl -sf -u "$AUTH" -H "$HOST" "$BASE/api/user/token/?name=nfc-dev-admin" \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["token"])')" || fail "could not get a token"
[ -n "$token" ] || fail "could not get a token"
printf '%s\n' "$token" > admin.token

say "collecting static files (the web UI's own, and the plugins')"
docker compose exec -T inventree-server invoke static > /dev/null || fail "invoke static failed"

# The server process that came up with `docker compose up` cannot see the plugin until it
# restarts (the editable install lands after it starts), so the restart comes before the
# activation, which would otherwise answer 404.
say "restarting the server and worker so the plugin's module is loaded"
docker compose restart inventree-server inventree-worker > /dev/null
wait_for "the server to come back" curl -sf -o /dev/null -H "$HOST" "$BASE/api/"

say "activating the plugin"
api -X PATCH "$BASE/api/plugins/nfcscanner/activate/" -d '{"active":true}' || fail "could not activate the plugin"

say "restarting once more so the plugin's URLs, tables and panels are served"
docker compose restart inventree-server inventree-worker > /dev/null
wait_for "the plugin's API" curl -sf -o /dev/null -H "$HOST" -H "Authorization: Token $(cat admin.token)" "$BASE/plugin/nfcscanner/api/scanners/"
say "ready: $INVENTREE_SITE_URL (login $INVENTREE_ADMIN_USER / the password in dev/.env)"

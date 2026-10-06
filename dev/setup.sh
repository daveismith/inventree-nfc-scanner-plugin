#!/bin/sh
# One-time (and idempotent) setup of the local InvenTree after `docker compose up -d`:
# the plugin integrations InvenTree needs, the base URL, the plugin's activation, and a
# restart so that its database tables and static files are in place. See README.md.
set -e
cd "$(dirname "$0")"

. ./.env
BASE="http://127.0.0.1:${INVENTREE_HTTP_PORT:-8080}"
HOST="Host: ${INVENTREE_SITE_URL#http://}"
AUTH="$INVENTREE_ADMIN_USER:$INVENTREE_ADMIN_PASSWORD"

say() { printf '%s\n' "$*"; }

say "waiting for InvenTree at $INVENTREE_SITE_URL ..."
for i in $(seq 1 60); do
    if curl -s -o /dev/null -H "$HOST" "$BASE/api/"; then break; fi
    sleep 5
done

say "enabling plugin integrations"
for key in ENABLE_PLUGINS_URL ENABLE_PLUGINS_APP ENABLE_PLUGINS_INTERFACE ENABLE_PLUGINS_EVENTS ENABLE_PLUGINS_SCHEDULE; do
    curl -s -o /dev/null -u "$AUTH" -H "$HOST" -H 'Content-Type: application/json' \
        -X PATCH "$BASE/api/settings/global/$key/" -d '{"value":"True"}'
done
curl -s -o /dev/null -u "$AUTH" -H "$HOST" -H 'Content-Type: application/json' \
    -X PATCH "$BASE/api/settings/global/INVENTREE_BASE_URL/" -d "{\"value\":\"$INVENTREE_SITE_URL\"}"

say "activating the plugin"
curl -s -o /dev/null -u "$AUTH" -H "$HOST" -H 'Content-Type: application/json' \
    -X PATCH "$BASE/api/plugins/nfcscanner/activate/" -d '{"active":true}'

say "an admin API token, in admin.token"
curl -s -u "$AUTH" -H "$HOST" "$BASE/api/user/token/?name=nfc-dev-admin" \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["token"])' > admin.token

say "collecting static files (the web UI's own, and the plugins')"
docker compose exec -T inventree-server invoke static > /dev/null

say "restarting the server and worker so the plugin's app is loaded"
docker compose restart inventree-server inventree-worker > /dev/null
for i in $(seq 1 60); do
    if curl -s -o /dev/null -H "$HOST" -H "Authorization: Token $(cat admin.token)" "$BASE/plugin/nfcscanner/api/scanners/"; then
        say "ready: $INVENTREE_SITE_URL (login $INVENTREE_ADMIN_USER / $INVENTREE_ADMIN_PASSWORD)"
        exit 0
    fi
    sleep 5
done
say "the server did not come back; see: docker compose logs inventree-server"
exit 1

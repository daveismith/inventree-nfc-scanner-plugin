# A local InvenTree for developing the plugin

This runs the InvenTree version the plugin targets (1.4.3) in Docker, with the plugin
installed from this source tree, so changes can be tried without touching the real server.

Needs Docker Desktop. The data (database, media, logs, the Python environment with the
plugin in it) lives in `dev/inventree-data/`, which is ignored by git.

`dev/.env` is the compose environment (InvenTree's standard variables: the version tag, the
proxy port, database and cache settings, and the first-run admin user). It is not in git;
copy InvenTree's `contrib/container/.env` and set `INVENTREE_TAG=1.4.3`,
`INVENTREE_SITE_URL=http://inventree.localhost:8080`, `INVENTREE_EXT_VOLUME=./inventree-data`
and `INVENTREE_PLUGINS_ENABLED=True`. The `setup.sh` script reads the admin user from it.

## Start

```sh
cd dev
docker compose up -d
./setup.sh
```

The first start pulls the images and runs InvenTree's setup (a minute or two). `setup.sh`
then turns on the plugin integrations InvenTree needs, sets the base URL, activates the
plugin and restarts the server so its database tables and static files are in place. It is
safe to run again.

Then open <http://inventree.localhost:8080> and log in as `admin` / `admin-nfc-dev`.

The proxy answers on any host name, not only `inventree.localhost`, so a scanner on the LAN
can reach this instance as `http://<this machine's IP>:8080/plugin/nfcscanner` (the firmware's
`CONFIG_APP_NET_ALLOW_HTTP` must be on for a plain-http URL).

## How the plugin gets in

`inventree-data/plugins.txt` holds one line, `-e /home/inventree/plugins/nfcscanner-src`,
and the compose file mounts this repository there. At every start InvenTree runs
`pip install -r plugins.txt`, which installs the plugin editable from the mount.

- **Python changes** take effect after `docker compose restart inventree-server inventree-worker`.
  The same restart is needed after a fresh `docker compose up` from stopped: the server
  process starts before the editable install from `plugins.txt` is visible to it, and
  until then the registry reports "No module named 'inventree_nfc_scanner'" and every
  plugin URL redirects to `/web` (a POST there fails CSRF). `setup.sh` does that restart.
- **Frontend changes** need `npm run build` in `frontend/` (which writes to
  `inventree_nfc_scanner/static/`) and then the same restart, which re-collects static files.
  For live reloading instead, see "Frontend development" in the main README.
- **A new migration** is applied at the restart too.
- **A panel or dashboard script that answers 404** after restarting the server and the worker
  together: both copy the plugin's static files at once and can leave a hashed bundle under a
  suffixed name. `docker exec nfcdev-server invoke static` puts it right.

## Try the API

`setup.sh` leaves an admin API token in `dev/admin.token`. For example:

```sh
TOKEN=$(cat admin.token)
curl -H "Authorization: Token $TOKEN" http://inventree.localhost:8080/plugin/nfcscanner/api/scanners/
```

Checking the plugin is the test suite's job: `tests/run.sh` (the server side) and
`tests/browser/run.sh` (the browser side, with a simulated scanner), with no need for this
instance. See [tests/README.md](../tests/README.md). This instance is for trying the plugin by
hand, and with a real scanner.

`usb_update.py --port /dev/cu.usbmodem…` does what the browser does when it connects to a
USB scanner with an update waiting: check in, claim, fetch, stream it over serial, and check
in again after the restart. Deploy something to the scanner first (a development build
packaged with the firmware's `tools/make_release.py --dev`, uploaded on the fleet item), and
take the scanner off the network (`nfcprog.py net disable`) if the network route should not
get there first.

## A real scanner against this instance

`add_scanner.py` sets a scanner up as a network scanner here: a user for it with an API
token, and an NFC Scanner machine with its reader id (`nfcprog.py info` shows it; the default
is the desk scanner's). It is safe to run again, and leaves the token in `scanner.token`:

```sh
python3 add_scanner.py [--reader nfc-34b7da52a084]
```

Then either give the scanner this server over USB, for its own Wi-Fi link
(`nfcprog.py net server http://<this machine's address>:8080/plugin/nfcscanner`, which asks
for the token; a development build is needed for http), or, with the scanner on USB, let the
firmware repository's `tools/sync_bridge.py` present it as a network scanner:

```sh
python3 ../../inventree_nfc_scanner/tools/sync_bridge.py \
    --url http://inventree.localhost:8080/plugin/nfcscanner --token $(cat scanner.token)
```

The scanner then shows online on the dashboard and in the NFC tag panel of any stock
location, and a job queued there is written by the real scanner.

## Stop and reset

```sh
docker compose down          # stop; data kept
docker compose down -v       # stop and forget everything (then delete inventree-data/)
```

"""Fixtures for the browser layer: run inside the `tests` container of compose.yaml.

InvenTree is at INVENTREE_URL on the stack's private network. Data is made through its REST
API as the admin; the browser is Playwright's Chromium (pytest-playwright's `page`), with
WebSerial replaced by webserial.js and a Python scanner model behind it.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import subprocess
import uuid
from pathlib import Path

import pytest
import requests
from playwright.sync_api import Page, expect

from .usb_scanner import Tag, UsbScanner

URL = os.environ.get("INVENTREE_URL", "http://localhost:8000")
ADMIN = (
    os.environ.get("INVENTREE_ADMIN_USER", "admin"),
    os.environ.get("INVENTREE_ADMIN_PASSWORD", "admin-browser-tests"),
)
P = "/plugin/nfcscanner"
HERE = Path(__file__).parent
RESULTS = Path("test-results/browser")

_counter = itertools.count(1)
RUN = uuid.uuid4().hex[
    :6
]  # names made by this run, apart from an earlier run's on the same stack

# The app's first load in a fresh container takes a while; after that, a few seconds is ample.
expect.set_options(timeout=20_000)

__all__ = ["P", "Tag"]


# The browser ---------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def base_url():
    return URL


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {**browser_context_args, "viewport": {"width": 1280, "height": 720}}


# The REST API, as the admin -----------------------------------------------------------------


class Api:
    def __init__(self, token):
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Token {token}",
            "Accept": "application/json",
        })

    def call(self, method, path, expect_status=None, **kw):
        r = self.s.request(method, URL + path, timeout=30, **kw)
        if expect_status is not None:
            assert r.status_code == expect_status, (
                method,
                path,
                r.status_code,
                r.text[:500],
            )
        else:
            assert r.ok, (method, path, r.status_code, r.text[:500])
        return (
            r.json()
            if r.content and "json" in r.headers.get("Content-Type", "")
            else None
        )

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, json=body, **kw)

    def patch(self, path, body=None, **kw):
        return self.call("PATCH", path, json=body, **kw)


@pytest.fixture(scope="session")
def api():
    r = requests.get(
        f"{URL}/api/user/token/?name=browser-tests", auth=ADMIN, timeout=30
    )
    assert r.ok, r.text
    return Api(r.json()["token"])


@pytest.fixture
def plugin_setting(api):
    """Change a plugin setting for one test; it goes back afterwards (the stack lives for the
    whole session)."""
    saved = {}

    def set_(key, value):
        if key not in saved:
            got = api.get(f"/api/plugins/nfcscanner/settings/{key}/")
            # A protected setting reads back masked; the tests leave those at their default.
            saved[key] = (
                "" if got.get("protected") or "*" in str(got["value"]) else got["value"]
            )
        api.patch(f"/api/plugins/nfcscanner/settings/{key}/", {"value": value})

    yield set_
    for key, value in saved.items():
        api.patch(f"/api/plugins/nfcscanner/settings/{key}/", {"value": value})


@pytest.fixture
def location(api):
    """A fresh stock location (a bin)."""
    return make_location(api)


def make_location(api, name="Bin"):
    return api.post(
        "/api/stock/location/",
        {"name": f"{name} {next(_counter)}-{RUN}"},
        expect_status=201,
    )


# The scanner on USB --------------------------------------------------------------------------


def _bridge(context, scanner: UsbScanner, granted: bool):
    context.expose_function("__serial_open", lambda: scanner.opened())
    context.expose_function("__serial_close", lambda: scanner.closed())
    context.expose_function("__serial_write", lambda text: scanner.feed(text))
    context.expose_function("__serial_read", lambda: scanner.drain())
    context.add_init_script(path=str(HERE / "webserial.js"))
    if granted:
        context.add_init_script(script="window.__serialShim.grant();")


@pytest.fixture
def usb(context, request):
    """A scanner on USB that the user chose on an earlier visit, so the page connects to it by
    itself. Its transcript is kept with a failed test's results."""
    scanner = UsbScanner()
    _bridge(context, scanner, granted=True)
    yield scanner
    _keep_transcript(request, scanner)


@pytest.fixture
def usb_new(context, request):
    """A scanner on USB the user has not chosen yet: "Connect scanner" asks for it."""
    scanner = UsbScanner()
    _bridge(context, scanner, granted=False)
    yield scanner
    _keep_transcript(request, scanner)


def _keep_transcript(request, scanner):
    report = getattr(request.node, "rep_call", None)
    if report is not None and report.failed:
        out = RESULTS / _slug(request.node.nodeid)
        out.mkdir(parents=True, exist_ok=True)
        (out / "serial.txt").write_text(
            "".join(f"{d} {line}\n" for d, line in scanner.transcript)
        )


def open_panel(page: Page, location: dict):
    """The location's NFC tag panel."""
    page.goto(f"/web/stock/location/{location['pk']}/nfc-tag")
    expect(page.get_by_text("Scanner on this computer")).to_be_visible()


def usb_badge(page: Page):
    """The USB route's status badge on the panel."""
    return page.get_by_text("Scanner on this computer").locator("xpath=following::*[1]")


def unplug(page: Page):
    page.evaluate("window.__serialShim.unplug()")


def replug(page: Page):
    page.evaluate("window.__serialShim.replug()")


# Logging in ----------------------------------------------------------------------------------


def log_in(page: Page, user=ADMIN[0], password=ADMIN[1], attempts=3):
    """Log in through the login form. InvenTree sometimes accepts a login (200) whose session
    does not hold, the next request being anonymous: on a server just started, and after its
    plugin registry reloads (a machine added, say). The form is then simply still there, and
    another go works."""
    for attempt in range(attempts):
        page.goto("/web/login")
        # The aria-labels InvenTree's own Playwright tests use.
        page.get_by_label("login-username").fill(user)
        page.get_by_label("login-password").fill(password)
        page.get_by_role("button", name="Log In").click()
        try:
            expect(page).not_to_have_url(re.compile("/login"), timeout=8_000)
            return
        except AssertionError:
            if attempt == attempts - 1:
                raise


@pytest.fixture(scope="session")
def stack(browser):
    """The stack, up and accepting logins (the first after start-up does not hold; see
    log_in)."""
    context = browser.new_context(base_url=URL)
    try:
        log_in(context.new_page(), attempts=5)
    except AssertionError:
        pytest.fail(
            "could not log in to InvenTree at all; see test-results/browser-stack-*.log"
        )
    finally:
        context.close()
    return URL


@pytest.fixture
def admin_page(stack, page: Page):
    """A page logged in as the admin."""
    log_in(page)
    return page


@pytest.fixture
def user_with(api):
    """`user_with("stock_location.change", ...)`: a user with those roles; returns
    {"name", "password", "pk"}."""

    def make(*roles):
        name = f"user{next(_counter)}x{RUN}"
        password = "browser-tests-pw-1"
        user = api.post(
            "/api/user/",
            {
                "username": name,
                "first_name": name,
                "last_name": "Test",
                "email": f"{name}@example.com",
                "password": password,
            },
            expect_status=201,
        )
        api.call(
            "PUT",
            f"/api/user/{user['pk']}/set-password/",
            json={"password": password, "override_warning": True},
        )
        if roles:
            group = api.post(
                "/api/user/group/", {"name": f"{name}-group"}, expect_status=201
            )
            for role in roles:
                rule, perm = role.split(".")
                (ruleset,) = [
                    r
                    for r in api.get(f"/api/user/ruleset/?group={group['pk']}")
                    if r["name"] == rule
                ]
                api.patch(f"/api/user/ruleset/{ruleset['pk']}/", {f"can_{perm}": True})
            api.patch(f"/api/user/{user['pk']}/", {"group_ids": [group["pk"]]})
        return {"name": name, "password": password, "pk": user["pk"]}

    return make


# Results -------------------------------------------------------------------------------------


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)


def _slug(nodeid: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", nodeid).strip("-").lower()[:120]


GIF_FILTER = (
    "fps={fps},scale=800:-1:flags=lanczos,split[a][b];"
    "[a]palettegen=max_colors=64:stats_mode=diff[p];"
    "[b][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle"
)
GIF_MAX = 5 * 1024 * 1024


def pytest_sessionfinish(session):
    """A failed test's video (pytest-playwright keeps only those) becomes an animated GIF, small
    enough to keep as an artifact; the WebM goes. Too large even at half the frame rate, and
    only the trace is kept, which holds the same frames."""
    for webm in sorted(Path("test-results").rglob("*.webm")):
        gif = webm.with_suffix(".gif")
        for fps in (6, 3):
            done = subprocess.run(
                [
                    "ffmpeg",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(webm),
                    "-vf",
                    GIF_FILTER.format(fps=fps),
                    str(gif),
                ],
                check=False,
            )
            if done.returncode == 0 and gif.stat().st_size <= GIF_MAX:
                break
            gif.unlink(missing_ok=True)
        webm.unlink()


# A scanner on the network ------------------------------------------------------------------


class NetScanner:
    """A network scanner calling /sync over HTTP with its user's token (the server suite's
    FakeScanner, over the wire)."""

    def __init__(self, token, reader, machine):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Token {token}"})
        self.reader, self.machine = reader, machine
        self.boot, self.seq, self.acked, self.fw = 1000, 0, 0, "1.0.0"

    def sync(self, *msgs, **body):
        numbered = []
        for m in msgs:
            self.seq += 1
            numbered.append({"seq": self.seq, **m})
        payload = {
            "reader": self.reader,
            "boot": self.boot,
            "proto": 1,
            "ack": self.acked,
            "wait_s": 0,
            "fw": self.fw,
            "msgs": numbered,
            **body,
        }
        r = self.s.post(f"{URL}{P}/sync/", json=payload, timeout=30)
        assert r.ok, r.text
        return r.json()["cmds"]

    def ack(self, cmd):
        self.acked = max(self.acked, cmd["seq"])


@pytest.fixture
def net_scanner(api, user_with):
    """`net_scanner(name=...)`: an active network scanner machine with a user of its own, and
    a NetScanner playing it. Deactivated afterwards, so later tests do not see it."""
    made = []

    def make(name=None):
        user = user_with()
        r = requests.get(
            f"{URL}/api/user/token/?name=scanner",
            auth=(user["name"], user["password"]),
            timeout=30,
        )
        assert r.ok, r.text
        reader = f"nfc-{uuid.uuid4().hex[:12]}"
        machine = api.post(
            "/api/machine/",
            {
                "name": name or f"Scanner {next(_counter)}-{RUN}",
                "machine_type": "nfc-scanner",
                "driver": "nfc-network",
                "active": True,
            },
            expect_status=201,
        )
        for key, value in (("READER_ID", reader), ("USER", str(user["pk"]))):
            api.call(
                "PUT",
                f"/api/machine/{machine['pk']}/settings/D/{key}/",
                json={"value": value},
            )
        api.post(f"/api/machine/{machine['pk']}/restart/")
        made.append(machine["pk"])
        return NetScanner(r.json()["token"], reader, machine)

    yield make
    for pk in made:
        api.patch(f"/api/machine/{pk}/", {"active": False})


def show_dashboard(page: Page, api: Api, *keys: str):
    """Log in as the admin, to a dashboard with just the plugin's items `keys` on it.

    InvenTree keeps the selection in the user's profile and reads it at login, so it is set
    there first; that is steadier than the dashboard's own editor, whose saving races a
    reload. Each item gets a layout of its own, the full width and tall enough not to
    scroll, one under the other: with none saved, InvenTree draws a widget one column wide.
    Call it instead of `admin_page`."""
    widgets = [f"p-nfcscanner-{key}" for key in keys]
    layout = [
        {"i": w, "x": 0, "y": 8 * n, "w": 12, "h": 8} for n, w in enumerate(widgets)
    ]
    api.patch(
        "/api/user/me/profile/",
        {"widgets": {"widgets": widgets, "layouts": {"lg": layout, "md": layout}}},
    )
    log_in(page)
    page.goto("/web/home")
    expect(page.locator(".react-grid-item")).to_have_count(len(widgets))


# Firmware --------------------------------------------------------------------------------

_minor = itertools.count(int(uuid.uuid4().int % 9000) + 1000)


def version(patch=0):
    """A firmware version newer than any a scanner here runs (1.0.0), and unique to this
    run: the server keeps every release it has held."""
    return f"5.{next(_minor)}.{patch}"


def upload_release(api, rel, app=None):
    files = {
        "manifest": (
            "manifest.json",
            json.dumps(rel.manifest).encode(),
            "application/json",
        ),
        "app": (
            rel.app_file,
            rel.app if app is None else app,
            "application/octet-stream",
        ),
    }
    r = api.s.post(f"{URL}{P}/api/fleet/upload/", files=files, timeout=60)
    assert r.status_code == 201, r.text
    return r.json()


def deploy(api, fw, readers, **kw):
    out = api.post(
        f"{P}/api/fleet/deploy/", {"firmware": fw["id"], "scanners": readers, **kw}
    )
    return [r.get("deployment") for r in out["results"]]


def deployment(api, dep_id):
    return next(d for d in api.get(f"{P}/api/fleet/deployments/") if d["id"] == dep_id)


@pytest.fixture
def github(plugin_setting):
    """A stand-in for GitHub's releases API, served from this container. The server shares its
    network namespace (compose.yaml), so it reaches it at localhost. `github.publish(rel)`."""
    import http.server
    import threading

    releases, assets = [], {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/repos/check/firmware/releases":
                body, ctype = json.dumps(releases).encode(), "application/json"
            elif path in assets:
                body, ctype = assets[path], "application/octet-stream"
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    class GitHub:
        def publish(self, rel, prerelease=False):
            listed = []
            for name, data in (
                ("manifest.json", json.dumps(rel.manifest).encode()),
                (rel.app_file, rel.app),
            ):
                path = f"/assets/{rel.version}/{name}"
                assets[path] = data
                listed.append({
                    "name": name,
                    "size": len(data),
                    "url": base + path,
                    "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                })
            releases.insert(
                0,
                {
                    "tag_name": f"v{rel.version}",
                    "draft": False,
                    "prerelease": prerelease,
                    "html_url": f"https://github.com/check/firmware/releases/tag/v{rel.version}",
                    "published_at": "2026-10-08T00:00:00Z",
                    "assets": listed,
                },
            )

    plugin_setting("FIRMWARE_REPO", "check/firmware")
    plugin_setting("FIRMWARE_API", base)
    yield GitHub()
    server.shutdown()

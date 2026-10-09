"""/sync: who may call it, what it accepts, and the numbering both ways (docs/api.md)."""

import pytest

from .scanner import P, FakeScanner


# Access ------------------------------------------------------------------------------------


def test_a_wrong_token_is_401(scanner, api):
    scanner.client = api(token="inv-not-a-token")
    assert scanner.sync()[0] == 401


def test_a_token_that_is_not_the_machines_user_is_403(scanner, api, admin_user):
    scanner.client = api(admin_user)
    assert scanner.sync()[0] == 403


def test_an_unknown_reader_gets_the_same_403(scanner):
    scanner.reader = "nfc-000000000000"
    assert scanner.sync()[0] == 403


def test_two_active_machines_with_one_reader_id_sync_neither(
    scanner, network_scanner, nobody
):
    network_scanner(reader=scanner.reader, user=nobody)
    assert scanner.sync()[0] == 403


def test_basic_auth_is_not_accepted(scanner, nobody):
    import base64

    from rest_framework.test import APIClient

    client = APIClient(SERVER_NAME="inventree.test", HTTP_ACCEPT="application/json")
    client.credentials(
        HTTP_AUTHORIZATION="Basic "
        + base64.b64encode(f"{nobody.username}:x".encode()).decode()
    )
    scanner.client = client
    assert scanner.sync()[0] == 401


# What it accepts ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body, field",
    [
        ({"boot": "x"}, "boot"),
        ({"msgs": "nope"}, "msgs"),
        ({"proto": "1"}, "proto"),
        ({"ack": -1}, "ack"),
        ({"wait_s": 2**31}, "wait_s"),
        ({"msgs": [1, 2]}, "msgs"),
    ],
)
def test_a_malformed_body_is_400(scanner, body, field):
    status, data = scanner.sync(**body)
    assert status == 400, data
    assert field in str(data) or "whole numbers" in str(data)


def test_reader_is_required(scanner):
    assert scanner.sync(raw={"proto": 1, "boot": 1})[0] == 400


def test_boot_is_required(scanner):
    status, data = scanner.sync(
        raw={"reader": scanner.reader, "proto": 1, "ack": 0, "msgs": []}
    )
    assert status == 400 and "boot" in str(data)


def test_whole_numbers_written_as_floats_are_accepted(scanner):
    assert scanner.sync(boot=7.0, ack=0.0)[0] == 200


# The exchange ------------------------------------------------------------------------------


def test_idle_sync_has_nothing_and_shows_online(scanner, api, clerk):
    assert scanner.cmds() == []
    (sc,) = [
        s
        for s in api(clerk).get(f"{P}/api/scanners/").json()
        if s["id"] == str(scanner.machine.pk)
    ]
    assert sc["online"] and sc["status"] == "online"
    assert sc["reader"] == scanner.reader


def test_an_unacknowledged_command_is_repeated(scanner, job_for):
    job_for(scanner)
    first = scanner.cmds()
    assert scanner.cmds() == first
    scanner.ack(first[0])
    assert scanner.cmds() == []


def test_messages_are_acknowledged_and_applied_once(scanner, job_for, api, clerk):
    job = job_for(scanner)
    (cmd,) = scanner.cmds()
    scanner.ack(cmd)
    done = {
        "evt": "done",
        "id": job["id"],
        "uid": "04A1B2C3D4E5F6",
        "type": "ntag215",
        "protected": False,
    }
    scanner.sync({"rsp": "program", "ok": True, "id": job["id"]})
    status, body = scanner.sync(dict(done, seq=10))
    assert body == {"ack": 10, "cmds": []}
    finished = api(clerk).get(f"{P}/api/jobs/{job['id']}/").json()["finished_at"]
    status, body = scanner.sync(dict(done, seq=10))
    assert body["ack"] == 10
    assert (
        api(clerk).get(f"{P}/api/jobs/{job['id']}/").json()["finished_at"] == finished
    )


def test_the_same_seq_after_a_restart_is_a_new_message(scanner, api, clerk):
    tap = {"evt": "tag", "uid": "04AABBCCDDEEFF", "type": "ntag215", "text": "INV-SL1"}
    scanner.sync(dict(tap, seq=1))
    scanner.restart()
    scanner.sync(dict(tap, seq=1, text="INV-SL2"))
    (sc,) = [
        s
        for s in api(clerk).get(f"{P}/api/scanners/").json()
        if s["id"] == str(scanner.machine.pk)
    ]
    assert sc["last_tag"]["text"] == "INV-SL2"


def test_an_answer_carries_at_most_two_commands(scanner):
    from inventree_nfc_scanner.sync import enqueue

    for i in range(4):
        enqueue(scanner.machine.machine_config, {"cmd": "info", "id": 7000 + i})
    cmds = scanner.cmds()
    assert [c["id"] for c in cmds] == [7000, 7001]
    scanner.ack(cmds[-1])
    assert [c["id"] for c in scanner.cmds()] == [7002, 7003]


def test_numbering_never_reuses_an_acknowledged_seq(scanner):
    from inventree_nfc_scanner.models import ScannerCommand
    from inventree_nfc_scanner.sync import enqueue

    config = scanner.machine.machine_config
    first = enqueue(config, {"cmd": "info"})
    scanner.ack(first.seq)
    scanner.cmds()
    ScannerCommand.objects.filter(machine=config).delete()  # bookkeeping cleared away
    assert enqueue(config, {"cmd": "info"}).seq > first.seq


def test_a_tap_outside_a_job_is_the_last_tag(scanner, api, clerk):
    scanner.sync({
        "evt": "tag",
        "uid": "04AABBCCDDEEFF",
        "type": "ntag215",
        "text": "INV-SL4",
        "protected": True,
    })
    (sc,) = [
        s
        for s in api(clerk).get(f"{P}/api/scanners/").json()
        if s["id"] == str(scanner.machine.pk)
    ]
    assert sc["last_tag"]["text"] == "INV-SL4"
    assert sc["last_tag"]["uid"] == "04AABBCCDDEEFF"


def test_a_message_that_fails_to_apply_is_still_acknowledged(scanner, monkeypatch):
    from inventree_nfc_scanner import sync

    def broken(*a, **k):
        raise RuntimeError("a bug")

    monkeypatch.setattr(sync, "apply_message", broken)
    status, body = scanner.sync({"evt": "tag", "uid": "04AABBCCDDEEFF"})
    assert status == 200 and body["ack"] == 1


def test_sync_records_the_firmware_version(scanner):
    from inventree_nfc_scanner.models import Scanner

    scanner.fw = "1.2.3"
    scanner.sync()
    assert Scanner.objects.get(reader_id=scanner.reader).fw == "1.2.3"


# Long polling ------------------------------------------------------------------------------


@pytest.fixture
def hold(monkeypatch):
    """The hold's clock and sleep, made virtual: `hold.at(seconds, fn)` runs fn when the held
    call has waited that long; `hold.waited` is how long it held."""
    from inventree_nfc_scanner import sync

    class Clock:
        now = 1000.0
        waited = 0.0
        events = []

        def monotonic(self):
            return self.now

        def sleep(self, s):
            self.now += s
            self.waited += s
            for when, fn in list(self.events):
                if self.waited >= when:
                    self.events.remove((when, fn))
                    fn()

        def at(self, when, fn):
            self.events.append((when, fn))

    clock = Clock()
    clock.events = []
    monkeypatch.setattr(sync, "time", clock)
    return clock


def test_a_held_call_returns_a_command_as_it_is_queued(scanner, set_setting, hold):
    from inventree_nfc_scanner.sync import enqueue

    set_setting("LONG_POLL", True)
    # Queued from "another process" two seconds into the hold (a request made from inside
    # the held one would be a nested request, which InvenTree's request logging cannot take).
    hold.at(
        2.0, lambda: enqueue(scanner.machine.machine_config, {"cmd": "info", "id": 42})
    )
    cmds = scanner.cmds(wait_s=20)
    assert [c["id"] for c in cmds] == [42]
    assert 2.0 <= hold.waited < 2.5


def test_an_empty_hold_ends_at_wait_s(scanner, set_setting, hold):
    set_setting("LONG_POLL", True)
    assert scanner.cmds(wait_s=3) == []
    assert 3.0 <= hold.waited < 3.5


def test_the_hold_is_capped_by_the_setting(scanner, set_setting, hold):
    set_setting("LONG_POLL", True)
    set_setting("LONG_POLL_MAX_S", 5)
    scanner.cmds(wait_s=60)
    assert 5.0 <= hold.waited < 5.5


def test_without_long_polling_there_is_no_hold(scanner, hold):
    assert scanner.cmds(wait_s=20) == []
    assert hold.waited == 0


def test_fake_scanner_numbers_its_messages():
    """The test's own scanner: messages numbered per boot, from 1 again after a restart."""
    sent = []

    class Client:
        def post(self, url, payload, format):
            sent.append(payload)

            class R:
                status_code = 200
                content = b"{}"

                def get(self, header):
                    return "application/json"

                def json(self):
                    return {"ack": 0, "cmds": []}

            return R()

    s = FakeScanner(Client(), reader="nfc-1")
    s.sync({"evt": "a"}, {"evt": "b"})
    s.restart()
    s.sync({"evt": "c"})
    assert [m["seq"] for m in sent[0]["msgs"]] == [1, 2]
    assert sent[1]["msgs"][0]["seq"] == 1 and sent[1]["boot"] == sent[0]["boot"] + 1

"""The scanner on the other end of the simulated WebSerial port (webserial.js).

A model of the firmware's serial protocol (newline-delimited JSON; the firmware's
`components/proto` and `docs/inventree-plugin-plan.md`), with state a test sets: what tag is
on the reader, what it holds, errors to inject. Fast and deterministic; the real firmware
(its host simulator) takes this one's place in the firmware-in-the-loop layer.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from dataclasses import dataclass, field


@dataclass
class Tag:
    uid: str = "04A1B2C3D4E5F6"
    type: str = "ntag215"
    text: str | None = None  # the Text record, if any
    uri: str | None = None  # the URI record, if any
    ndef: str | None = None  # the message last written, as hex
    protected: bool = False
    pwd: str | None = None

    @property
    def blank(self) -> bool:
        return self.ndef is None and self.text is None and self.uri is None


@dataclass
class UsbScanner:
    # Its own per test: the server's scanner registry lasts the whole session.
    reader: str = field(default_factory=lambda: f"nfc-{uuid.uuid4().hex[:12]}")
    fw: str = "1.0.0"
    proto: int = 1
    tag: Tag | None = None  # on the reader now
    hid: bool = True
    refuse: dict = field(default_factory=dict)  # command -> error, to refuse it with
    transcript: list = field(
        default_factory=list
    )  # ("<"|">", line), every line both ways
    restarts: int = 0
    next_fw: str | None = None  # what it runs after an update that ends well

    def __post_init__(self):
        self._out: list[str] = []
        self._in = ""
        self.job: dict | None = None  # the program or wipe job waiting for a tag
        self.ota: dict | None = None
        self._restart = False

    # The port's side (called through the bridge) ----------------------------------------

    def opened(self):
        self._in = ""

    def closed(self):
        if self.job:  # the firmware ends a job when its link goes
            self.job = None

    def feed(self, text: str) -> str:
        """Take what the page wrote. Returns "restart" if the scanner restarts as a result,
        which the port then shows as an unplug and a replug."""
        self._restart = False
        self._in += text
        while "\n" in self._in:
            line, self._in = self._in.split("\n", 1)
            if line.strip():
                self.transcript.append((">", line))
                self._handle(line)
        return "restart" if self._restart else ""

    def drain(self) -> str:
        out, self._out = "".join(self._out), []
        return out

    # The test's side ---------------------------------------------------------------------

    def present(self, tag: Tag | None = None):
        """Put a tag on the reader: a job waiting for one gets it, otherwise it is a tap."""
        self.tag = tag or Tag()
        if self.job:
            self._run_job()
        else:
            self._say(evt="tag", **self._tag_fields(self.tag))

    def remove(self):
        if self.tag:
            self._say(evt="tag_removed", uid=self.tag.uid)
        self.tag = None

    def lines(self, direction=">"):
        """The messages that went one way, parsed: ">" what the page sent."""
        return [json.loads(text) for d, text in self.transcript if d == direction]

    def commands(self, name):
        return [m for m in self.lines(">") if m.get("cmd") == name]

    # The firmware's behaviour ------------------------------------------------------------

    def _say(self, **msg):
        line = json.dumps(msg)
        self.transcript.append(("<", line))
        self._out.append(line + "\n")

    def _tag_fields(self, tag: Tag) -> dict:
        out = {"uid": tag.uid, "type": tag.type, "protected": tag.protected}
        if tag.text:
            out["text"] = tag.text
        if tag.uri:
            out["uri"] = tag.uri
        return out

    def _handle(self, line: str):
        try:
            cmd = json.loads(line)
        except ValueError:
            return self._say(evt="error", error="bad_json")
        name = cmd.get("cmd")
        if name in self.refuse:
            return self._say(
                rsp=name, ok=False, id=cmd.get("id"), error=self.refuse[name]
            )
        handler = getattr(self, f"_cmd_{name}", None)
        if handler is None:
            return self._say(evt="error", error="unknown_cmd", detail=str(name))
        handler(cmd)

    def _cmd_info(self, cmd):
        self._say(
            rsp="info",
            ok=True,
            proto=self.proto,
            fw=self.fw,
            reader=self.reader,
            idf="v6.1",
            pn532={"ic": 50, "ver": "1.6"},
            state="waiting" if self.job else "idle",
            job=self.job["id"] if self.job else None,
            hid=self.hid,
            buzzer=False,
            reset="poweron",
            crash=None,
            **({"tag": self.tag.uid} if self.tag else {}),
        )

    def _cmd_hid(self, cmd):
        self.hid = bool(cmd.get("enabled"))
        self._say(rsp="hid", ok=True, enabled=self.hid)

    def _cmd_program(self, cmd):
        if self.job:
            return self._say(rsp="program", ok=False, id=cmd.get("id"), error="busy")
        self.job = cmd
        self._say(rsp="program", ok=True, id=cmd["id"])
        self._say(evt="waiting", id=cmd["id"], timeout_ms=cmd.get("timeout_ms", 60000))
        if self.tag:
            self._run_job()

    def _cmd_cancel(self, cmd):
        if not self.job:
            return self._say(rsp="cancel", ok=False, error="no_job")
        job, self.job = self.job, None
        self._say(rsp="cancel", ok=True, id=job["id"])
        self._say(evt="failed", id=job["id"], error="cancelled")

    def _run_job(self):
        job, tag = self.job, self.tag
        self.job = None

        def fail(error, **more):
            self._say(evt="failed", id=job["id"], error=error, uid=tag.uid, **more)

        if not tag.blank and not job.get("overwrite"):
            return fail(
                "not_blank",
                **{k: v for k, v in (("text", tag.text), ("uri", tag.uri)) if v},
            )
        if tag.protected and job.get("pwd") != tag.pwd:
            return fail("auth_failed" if job.get("pwd") else "auth_required")
        self._say(evt="writing", id=job["id"], uid=tag.uid)
        tag.ndef = job["ndef"]
        tag.text, tag.uri = _records(bytes.fromhex(job["ndef"]))
        if job.get("pwd"):
            tag.protected, tag.pwd = True, job["pwd"]
        self._say(
            evt="done",
            id=job["id"],
            uid=tag.uid,
            type=tag.type,
            protected=tag.protected,
        )

    # Firmware updates over the link (the firmware's ota_stream)

    def _cmd_ota_begin(self, cmd):
        self.ota = {
            "id": cmd["id"],
            "size": cmd["size"],
            "sha256": cmd["sha256"],
            "data": bytearray(),
        }
        self._say(rsp="ota_begin", ok=True, id=cmd["id"])

    def _cmd_ota_data(self, cmd):
        ota = self.ota
        if not ota or cmd.get("id") != ota["id"] or cmd.get("at") != len(ota["data"]):
            return self._say(
                rsp="ota_data",
                ok=False,
                id=cmd.get("id"),
                error="bad_arg",
                detail="out of order",
            )
        ota["data"] += base64.b64decode(cmd["data"])
        self._say(rsp="ota_data", ok=True, id=cmd["id"])

    def _cmd_ota_end(self, cmd):
        ota, self.ota = self.ota, None
        if (
            not ota
            or len(ota["data"]) != ota["size"]
            or hashlib.sha256(ota["data"]).hexdigest() != ota["sha256"]
        ):
            return self._say(
                rsp="ota_end",
                ok=False,
                id=cmd.get("id"),
                error="bad_image",
                detail="sha256 mismatch",
            )
        self._say(rsp="ota_end", ok=True, id=cmd["id"])
        self.restarts += 1
        if self.next_fw:
            self.fw = self.next_fw
        self._restart = True


def _records(message: bytes):
    """The Text and URI of an NDEF message as plugin's ndef.py builds it (two short records)."""
    text = uri = None
    i = 0
    prefixes = {0x03: "http://", 0x04: "https://"}
    while i < len(message):
        header, type_len, payload_len = message[i], message[i + 1], message[i + 2]
        rtype = message[i + 3 : i + 3 + type_len]
        payload = message[i + 3 + type_len : i + 3 + type_len + payload_len]
        if rtype == b"T":
            lang = payload[0] & 0x3F
            text = payload[1 + lang :].decode()
        elif rtype == b"U":
            uri = prefixes.get(payload[0], "") + payload[1:].decode()
        i += 3 + type_len + payload_len
        if header & 0x40:  # ME
            break
    return text, uri

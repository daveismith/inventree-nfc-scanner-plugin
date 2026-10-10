"""The firmware's own code behind the simulated WebSerial port, in place of usb_scanner.py.

host_sim (the firmware repository's `host_sim`) is the scanner's state machine, protocol, tag
logic and OTA as a Linux program, with a simulated NTAG for the reader and a pty for USB.
SimScanner starts it and passes bytes between the page and its pty, with the same interface
as UsbScanner, so the bridge in conftest.py takes either.

Where it differs from the device, the bridge makes up the difference:
- Lines starting with '!' move the simulated tag (`!tag ntag215`, `!remove`, ...); their
  `{"sim": ...}` replies are taken off the stream before the page sees it.
- After an update the simulator "restarts" on the same pty (it says `restarting`); the port
  is shown to the page as unplugged and back, as the device's restart looks.

The binary: HOST_SIM, else tests/browser/.sim/host_sim (tests/browser/sim.sh builds it).
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import time
import tty
import uuid
from pathlib import Path

DEFAULT_BINARY = Path(__file__).parent / ".sim" / "host_sim"


def binary() -> Path | None:
    path = Path(os.environ.get("HOST_SIM", DEFAULT_BINARY))
    return path if path.is_file() and os.access(path, os.X_OK) else None


class SimScanner:
    def __init__(self, path: Path, reader: str | None = None, fw: str | None = None):
        self.reader = reader or f"nfc-{uuid.uuid4().hex[:12]}"
        env = dict(os.environ, SIM_READER=self.reader)
        if fw:
            env["SIM_FW"] = fw
        self.proc = subprocess.Popen(
            [str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=env,
        )
        pty = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and pty is None:
            line = self.proc.stdout.readline()
            if line.startswith("PTY "):
                pty = line.split()[1]
            elif not line and self.proc.poll() is not None:
                break
        if pty is None:
            self.proc.kill()
            raise RuntimeError(f"{path} did not report its pty")
        self.fd = os.open(pty, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        tty.setraw(self.fd)
        self.transcript: list[tuple[str, str]] = []
        self._partial = ""  # an incomplete line read from the simulator
        self._out = ""  # complete lines for the page
        self._sim_replies: list[dict] = []
        self.restarts = 0

    # The port's side ---------------------------------------------------------------------

    def opened(self):
        pass  # the simulator's link is up for as long as it runs

    def closed(self):
        pass

    def feed(self, text: str) -> str:
        for line in text.splitlines():
            if line.strip():
                self.transcript.append((">", line))
        os.write(self.fd, text.encode())
        if '"ota_end"' in text:
            # The simulator checks the image and restarts at once, or refuses.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                self._pump(0.1)
                if '"state":"restarting"' in self._out.replace(" ", ""):
                    self.restarts += 1
                    return "restart"
                if '"rsp":"ota_end"' in self._out.replace(" ", ""):
                    break
        return ""

    def drain(self) -> str:
        self._pump(0)
        out, self._out = self._out, ""
        return out

    # The test's side ---------------------------------------------------------------------

    def control(self, line: str):
        """A '!' line, answered {"sim":"ok"} (or "error")."""
        os.write(self.fd, (line + "\n").encode())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self._pump(0.05)
            if self._sim_replies:
                reply = self._sim_replies.pop(0)
                assert reply.get("sim") == "ok", (line, reply)
                return
        raise AssertionError(f"no answer from the simulator to {line}")

    def present(self, kind: str = "ntag215"):
        """A factory-fresh tag on the reader (ntag213, ntag215, ntag216, or classic)."""
        self.control(f"!tag {kind}")

    def tap(self):
        """The tag off the reader and back on, as it is."""
        self.control("!tap")

    def remove(self):
        self.control("!remove")

    def tear_after(self, writes: int):
        self.control(f"!tear {writes}")

    def lines(self, direction=">"):
        out = []
        for d, text in self.transcript:
            if d == direction:
                try:
                    out.append(json.loads(text))
                except ValueError:
                    pass
        return out

    def commands(self, name):
        return [m for m in self.lines(">") if m.get("cmd") == name]

    def events(self, name):
        return [m for m in self.lines("<") if m.get("evt") == name]

    def stop(self):
        self.proc.kill()
        self.proc.wait(timeout=5)
        os.close(self.fd)

    # ---------------------------------------------------------------------------------------

    def _pump(self, wait: float):
        """Read what the simulator has said; sort its lines into the page's and its replies."""
        while True:
            ready, _, _ = select.select([self.fd], [], [], wait)
            if not ready:
                return
            try:
                chunk = os.read(self.fd, 65536).decode(errors="replace")
            except BlockingIOError:
                return
            if not chunk:
                return
            self._partial += chunk
            while "\n" in self._partial:
                line, self._partial = self._partial.split("\n", 1)
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith('{"sim"'):
                    self._sim_replies.append(json.loads(stripped))
                    continue
                self.transcript.append(("<", stripped))
                self._out += line + "\n"
            wait = 0

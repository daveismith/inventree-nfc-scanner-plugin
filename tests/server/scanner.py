"""A network scanner as the plugin sees it: something that calls /sync.

It numbers its messages per boot, acknowledges what it has been sent, and can restart.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SLUG = "nfcscanner"
P = f"/plugin/{SLUG}"  # the plugin's URLs


@dataclass
class FakeScanner:
    client: object  # an APIClient with the scanner user's token
    reader: str
    boot: int = 1000
    fw: str | None = "1.0.0"
    acked: int = 0  # the highest command seq acknowledged
    seq: int = 0  # the last message seq used, this boot
    last: dict = field(default_factory=dict)
    machine: object = None  # the InvenTree machine it is configured as

    def sync(self, *msgs, wait_s=0, raw=None, **body):
        """One call. Messages without a `seq` are numbered. Returns (status, body)."""
        numbered = []
        for m in msgs:
            if "seq" not in m:
                self.seq += 1
                m = {"seq": self.seq, **m}
            numbered.append(m)
        payload = raw if raw is not None else {
            "reader": self.reader,
            "boot": self.boot,
            "proto": 1,
            "ack": self.acked,
            "wait_s": wait_s,
            "msgs": numbered,
            **({"fw": self.fw} if self.fw else {}),
            **body,
        }
        r = self.client.post(f"{P}/sync/", payload, format="json")
        data = r.json() if r.content else None
        self.last = data or {}
        return r.status_code, data

    def cmds(self, *msgs, **body):
        """Sync and return the commands, insisting on a 200."""
        status, data = self.sync(*msgs, **body)
        assert status == 200, data
        return data["cmds"]

    def ack(self, cmd_or_seq):
        """Acknowledge up to a command (sent with the next call)."""
        seq = cmd_or_seq["seq"] if isinstance(cmd_or_seq, dict) else cmd_or_seq
        self.acked = max(self.acked, seq)

    def restart(self):
        """A new boot: message numbering starts over and nothing is acknowledged yet."""
        self.boot += 1
        self.seq = 0
        self.acked = 0

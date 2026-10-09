#!/usr/bin/env python3
"""Propose the InvenTree versions to test against, from InvenTree's release tags on stdin.

The oldest supported version stays (it is the plugin's MIN_VERSION, raised by hand); after it
comes the newest patch of every later minor. Writes the new list to
tests/inventree-versions.json and prints it, or prints nothing if it has not changed.

    gh api repos/inventree/InvenTree/releases --paginate \
        --jq '.[] | select(.draft == false and .prerelease == false) | .tag_name' \
        | python3 tests/propose_versions.py
"""

import json
import re
import sys
from pathlib import Path

LIST = Path(__file__).parent / "inventree-versions.json"


def parse(tag):
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag.strip())
    return tuple(int(x) for x in m.groups()) if m else None


def propose(supported, tags):
    oldest = parse(supported[0])
    newest_patch = {}
    for v in filter(None, map(parse, tags)):
        if v[:2] > oldest[:2]:
            newest_patch[v[:2]] = max(newest_patch.get(v[:2], v), v)
    since = [v for _, v in sorted(newest_patch.items())]
    return [supported[0]] + [".".join(map(str, v)) for v in since]


def main():
    data = json.loads(LIST.read_text())
    proposed = propose(data["supported"], sys.stdin.read().split())
    if proposed != data["supported"]:
        data["supported"] = proposed
        LIST.write_text(json.dumps(data, indent=2) + "\n")
        print(" ".join(proposed))


if __name__ == "__main__":
    main()

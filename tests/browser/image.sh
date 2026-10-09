#!/bin/sh
# The browser layer's test image in GitHub's registry, tagged with a hash of what goes into it
# (tests.Dockerfile and requirements.txt): a change to either is a new tag, so a tag that is
# there is never out of date. CI builds and pushes a tag that is not there yet; tests/browser/
# run.sh pulls it.
#
#   tests/browser/image.sh          ghcr.io/<owner>/<repo>/browser-tests:<hash>
#   tests/browser/image.sh --hash   <hash>
set -eu

here=$(cd "$(dirname "$0")" && pwd)
repo=${BROWSER_IMAGE_REPO:-ghcr.io/daveismith/inventree-nfc-scanner-plugin/browser-tests}
hash=$(python3 -c 'import hashlib, sys
h = hashlib.sha256()
for name in sys.argv[1:]:
    h.update(open(name, "rb").read())
print(h.hexdigest()[:12])' "$here/tests.Dockerfile" "$here/requirements.txt")
if [ "${1:-}" = --hash ]; then echo "$hash"; else echo "$repo:$hash"; fi

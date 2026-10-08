# Firmware updates for scanners

An admin chooses a firmware release and the scanners that should run it. A scanner on the
network installs it on its next call to the server. A scanner on USB installs it the next time
a browser connects to it, either at once or when the user agrees, as the admin decides.

## Where releases come from

The scanner firmware's repository publishes a GitHub release for every version tag (`v1.2.3`,
or `v1.2.3-rc.1` for a pre-release). Each release carries three files:

| File | |
| --- | --- |
| `inventree_nfc_scanner-<version>.bin` | The app image: what an update installs |
| `inventree_nfc_scanner-<version>-merged.bin` | Everything from offset 0, for flashing a blank board with esptool |
| `manifest.json` | Version, sha256 and size of both, the scanner protocol, the network-settings layout, the oldest plugin it works with |

The plugin fetches new releases from the repository in the *Firmware repository* setting:
every *Check for firmware every* hours (24 by default; 0 for on demand only), and when an
admin presses *Check now*. Pre-releases are fetched only with *Include pre-releases* on. A
release is kept only if every file matches the manifest's sha256 and GitHub's own digest.
The repository is public, so no credential is needed; *GitHub token* exists only to raise
GitHub's rate limit (60 calls an hour per address; a check uses two or three).

A server without internet access takes the same three files by upload, checked the same way.

The images are kept in InvenTree's media storage, so its backups include them. Only the newest
*Firmware releases kept* (5) keep their images; older ones keep their record.

## Deploying

The *NFC scanner firmware* dashboard item, shown to admins only, lists every scanner the server
has heard of: network scanners from their calls, USB scanners from a browser connecting to
them. Choose scanners, a release, and *Deploy*. Each scanner gets its own deployment, and its
progress shows beside it and in the history.

- A release the plugin cannot drive (another scanner protocol, or one that needs a newer
  plugin) cannot be deployed.
- A scanner already running that version is skipped. Going to an older version needs *allow a
  downgrade*. A network scanner is never sent a version older than the layout of the network
  settings it keeps, since it would forget them and drop off the network.
- A second deployment to a scanner replaces one still pending.
- *Deploy new releases automatically* sends each new stable release to every scanner running
  something older (or a version it has not reported), as soon as it is fetched. It is off by
  default. It only ever sends the newest release this plugin can drive: a newer one that needs
  a newer plugin, or speaks another protocol, is fetched, listed and marked incompatible, but
  not deployed; update the plugin to use it. Whatever happens, the check itself is recorded,
  and a deployment refused is listed among its errors.
- The fleet page's "newest" release, and the "available" badge on a scanner, are likewise the
  newest this plugin can deploy.

Only admins may see or do any of this: superusers, and users whose group has change permission
on the *Admin* role.

## A network scanner

On the scanner's next call, once it has no job running, the server sends it the `ota` command
with the image's address on this server and its sha256. The scanner downloads it into its
second slot, checks the digest, and restarts into it. The new firmware is on trial: if it does
not reach the server within 15 minutes, the scanner goes back to the old one by itself. The
version it reports after the restart decides the outcome: *confirmed*, or *rolled back*.

With long polling on, the update starts within a second. *Network updates at once* (2) limits
how many scanners download at the same time, since each download holds a server worker. A
scanner busy with a job is asked again a minute later. One that restarts mid-download is
tried again, up to three times.

The scanner fetches the image with its own token, from the address it calls the server on.
Behind a proxy, that address must be the one the server sees; an update the scanner refuses as
not from its server means the proxy hides the scheme or host (`X-Forwarded-Proto`,
`X-Forwarded-Host`).

## A USB scanner

When a browser connects to the scanner (on a location's NFC tag panel or the dashboard), it
tells the server which scanner it is and what it runs. If an update is waiting, the browser
shows it. What happens then is the admin's choice, in the *USB update policy* setting and per
deployment:

- **Required**: the update installs at once; the panel cannot be used until it has.
- **Deferrable**: the user chooses *Update now* or *Later*. *Later* is recorded, and the offer
  returns on the next connection. A deployment can be given a date from which it is required.

Installing takes about half a minute for a 1.2 MB image: the browser fetches it from the
server, checks it, and sends it to the scanner over the serial link. The scanner restarts
into it and the browser reconnects by itself. The next check-in confirms it. An update cut
short (the page closed, the cable pulled) is offered again on the next connection; the scanner
keeps running its old firmware meanwhile.

A scanner on both USB and the network takes the update by whichever route reaches it first.

## Integrity, and what it does not cover

The image's sha256 travels from GitHub's digest, through the manifest and the server's check,
in the command or offer to the scanner, which checks what it wrote before it restarts into it.
A file swapped on the way, or on the server's disk after the check, is refused by the scanner.
There is no signature: a server that is itself compromised can send any image. Resisting that
needs secure boot, which the firmware does not have.

The image endpoint answers any signed-in user or API token; the images are public on GitHub.

## Trying it locally

`dev/check_fleet.py` exercises all of the above against the development instance, with a
scanner of its own and a stand-in for GitHub. `dev/usb_update.py --port <scanner>` does what
the browser does, against a real scanner on USB. A development build of the firmware (which
allows plain http) can be packaged for upload with `tools/make_release.py --dev` in the
firmware repository.

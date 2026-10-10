/**
 * The USB scanner as a service that outlives any one panel.
 *
 * InvenTree loads a plugin's JavaScript once per page load and keeps it while the user moves
 * around the app, so a connection held here survives navigation: the location page's panel
 * and the dashboard item both attach to it, and neither closes it when it unmounts. The
 * service also acts on taps by itself (a tag for another bin opens that bin), so that works
 * on any page once the module has loaded, which it has as soon as the dashboard or a
 * location page has been shown.
 *
 * It holds the port in one tab at a time, and only while that tab is visible, except that a
 * job in progress keeps it until the job ends.
 *
 * On connecting it checks the scanner in with the server, which may answer with a firmware
 * update an admin has deployed to it. A required one is installed at once; a deferrable one
 * waits for the user's "Update now" or "Later". Installing streams the image over the serial
 * link (ota_begin, ota_data, ota_end), the scanner restarts into it, the port comes back by
 * itself, and the check-in that follows tells the server, and the user, how it went.
 */
import type { InvenTreePluginContext } from '@inventreedb/ui';

import {
  fetchImage,
  type UpdateOffer,
  usbCheckIn,
  usbDefer,
  usbReport,
  usbStart
} from './api';
import { DEPLOY_CHANNEL } from './channel';
import {
  grantedPort,
  hasWebSerial,
  type LinkState,
  locationFromUri,
  requestPort,
  ScannerLink,
  type ScannerMessage
} from './serial';

type Navigate = (path: string) => void;
type Api = InvenTreePluginContext['api'];

const CHUNK = 768; // bytes of image per ota_data line (the firmware's APP_OTA_CHUNK_MAX)

/**
 * How often a connected scanner is checked in again, so an update deployed after it connected
 * is offered without a reload. The browser tests shorten it (window.__NFC_SCANNER_POLL_MS).
 */
const RECHECK_MS: number =
  (typeof window !== 'undefined' && (window as any).__NFC_SCANNER_POLL_MS) ||
  30000;

export interface UpdateProgress {
  version: string;
  stage: 'starting' | 'fetching' | 'sending' | 'checking' | 'restarting';
  done: number;
  total: number;
}

export interface ScannerState {
  link: LinkState;
  info: ScannerMessage | null; // the scanner's `info`, once connected
  onReader: ScannerMessage | null; // the tag on the reader, from `tag` events
  jobActive: boolean; // a job is running from some panel: taps are its business
  update: UpdateOffer | null; // a firmware update waiting for this scanner
  updating: UpdateProgress | null; // one being installed now
  updateNote: { ok: boolean; text: string } | null; // how the last one went
}

function bytesToBase64(bytes: Uint8Array): string {
  let s = '';
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s);
}

async function sha256Hex(bytes: Uint8Array): Promise<string | null> {
  if (!crypto?.subtle) return null; // not a secure context: the scanner checks it anyway
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest), (b) =>
    b.toString(16).padStart(2, '0')
  ).join('');
}

class ScannerService {
  private link: ScannerLink | null = null;
  private listeners = new Set<() => void>();
  private navigate: Navigate | null = null;
  private started = false;
  private connecting = false;

  state: ScannerState = {
    link: 'closed',
    info: null,
    onReader: null,
    jobActive: false,
    update: null,
    updating: null,
    updateNote: null
  };

  private api: Api | null = null;
  /** The update this tab installed, whose verdict the next check-in brings. */
  private awaitingVerdict: number | null = null;
  /** The update the user put off ("Later"): not offered again until the scanner reconnects. */
  private deferred: number | null = null;
  private recheckTimer = 0;

  /** React-style subscription (for useSyncExternalStore). */
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  getState = () => this.state;

  private set(patch: Partial<ScannerState>) {
    this.state = { ...this.state, ...patch };
    for (const l of this.listeners) l();
  }

  /**
   * Called by every plugin component as it mounts. Keeps the latest router `navigate`, and
   * on the first call starts the connection and the visibility handling.
   */
  attach(navigate: Navigate, api?: Api) {
    this.navigate = navigate;
    if (api && !this.api) {
      this.api = api;
      if (this.state.info) this.checkIn(this.state.info).catch(() => {});
    }
    if (this.started || !hasWebSerial()) return;
    this.started = true;
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'hidden') {
        // A job in progress keeps the port: it ends by itself, and the close follows then.
        if (this.state.jobActive) this.closeWhenIdle = true;
        else this.link?.close();
      } else {
        this.closeWhenIdle = false;
        this.reconnect().catch(() => {});
        this.recheck(); // back to the tab: anything deployed meanwhile is offered now
      }
    });
    // A deployment made on this plugin's fleet page, in this tab or another, is offered at
    // once rather than at the next periodic check-in.
    if (typeof BroadcastChannel !== 'undefined') {
      new BroadcastChannel(DEPLOY_CHANNEL).onmessage = (e) => {
        if (e.data?.type === 'deployed') this.recheck();
      };
    }
    // Plugged in or out: only the scanner matters, not any other serial device.
    navigator.serial?.addEventListener('disconnect', (e) => {
      if (this.port && (e.target as unknown) === this.port) this.link?.close();
    });
    navigator.serial?.addEventListener('connect', () => {
      this.reconnect().catch(() => {});
    });
    this.reconnect().catch(() => {});
  }

  private port: SerialPort | null = null;
  private closeWhenIdle = false;
  private retryTimer = 0;

  /** The link closed: a job that was running has no scanner any more. */
  onClose(listener: () => void): () => void {
    return this.ensureLink().onClose(listener);
  }

  private ensureLink(): ScannerLink {
    if (!this.link) {
      this.link = new ScannerLink();
      this.link.onState = (link) => {
        this.set({
          link,
          ...(link === 'closed'
            ? { info: null, onReader: null, update: null }
            : {})
        });
        if (link === 'open') this.startRechecks();
        else window.clearInterval(this.recheckTimer);
        // Another tab has the port: it lets go when hidden, or when its job ends, with no
        // word to us, so look again now and then.
        window.clearInterval(this.retryTimer);
        if (link === 'busy-elsewhere')
          this.retryTimer = window.setInterval(() => {
            if (this.state.link === 'busy-elsewhere')
              this.reconnect().catch(() => {});
          }, 5000);
      };
      this.link.onMessage = (msg) => this.onMessage(msg);
    }
    return this.link;
  }

  get isOpen(): boolean {
    return this.link?.isOpen ?? false;
  }

  /** Open a port the user granted earlier, if the tab is visible and nothing is open. */
  async reconnect() {
    if (
      document.visibilityState !== 'visible' ||
      this.isOpen ||
      this.connecting
    )
      return;
    if (this.link && this.link.state === 'busy-elsewhere')
      this.link.state = 'closed'; // try afresh
    const port = await grantedPort();
    if (port) await this.connect(port);
  }

  async connect(port: SerialPort, steal = false): Promise<void> {
    const link = this.ensureLink();
    if (link.isOpen || this.connecting) return;
    this.connecting = true;
    try {
      // Taking the port from another tab: that tab lets go when it loses the lock, which
      // takes a moment, so an open that fails is tried again briefly.
      let opened = false;
      for (let attempt = 0; attempt < (steal ? 6 : 1); attempt++) {
        try {
          opened = await link.open(port, steal);
          break;
        } catch (e) {
          if (attempt === (steal ? 5 : 0)) throw e;
          await new Promise((r) => setTimeout(r, 250));
        }
      }
      if (!opened) return;
      this.port = port;
      this.deferred = null; // a new connection: a put-off update is offered again
      await link.request({ cmd: 'hid', enabled: false }).catch(() => {});
      let info: ScannerMessage;
      try {
        info = await link.request({ cmd: 'info' });
      } catch (e) {
        await link.close(); // a port that does not answer is not a scanner we can use
        throw e;
      }
      this.set({ info, onReader: info.tag ? { uid: info.tag } : null });
    } finally {
      this.connecting = false;
    }
    if (this.state.info) this.checkIn(this.state.info).catch(() => {});
  }

  /**
   * Tell the server which scanner is here and what it runs; hear whether an update waits
   * for it. A user who may not program tags gets 403, and simply no offer. Firmware too old
   * to say who it is (no `reader` in `info`) cannot take an update over USB anyway.
   */
  private async checkIn(info: ScannerMessage) {
    if (!this.api || typeof info.reader !== 'string') return;
    const ci = await usbCheckIn(this.api, {
      reader: info.reader,
      fw: String(info.fw ?? ''),
      proto: Number(info.proto ?? 0)
    });
    if (
      this.awaitingVerdict !== null &&
      ci.last &&
      ci.last.id === this.awaitingVerdict
    ) {
      this.awaitingVerdict = null;
      const ok = ci.last.state === 'confirmed';
      this.set({
        updateNote: {
          ok,
          text: ok
            ? `Scanner updated to firmware ${ci.last.version}.`
            : ci.last.state === 'rolled_back'
              ? `The update to ${ci.last.version} did not hold; the scanner went back to ${info.fw}.`
              : `The update to ${ci.last.version} did not finish (${ci.last.error || ci.last.state}).`
        }
      });
    }
    const offer =
      ci.update && ci.update.id === this.deferred && !ci.update.required
        ? null // put off for this connection
        : ci.update;
    this.set({ update: offer });
    if (offer?.required && !this.state.updating) {
      this.installUpdate().catch(() => {});
    }
  }

  private startRechecks() {
    window.clearInterval(this.recheckTimer);
    this.recheckTimer = window.setInterval(() => this.recheck(), RECHECK_MS);
  }

  /**
   * Check the connected scanner in again, for an update deployed since it connected. Not while
   * an update is being installed or awaits its verdict: the server reads a check-in then as the
   * update cut off, or as the scanner back on its old firmware.
   */
  recheck() {
    if (
      !this.isOpen ||
      !this.state.info ||
      this.state.updating ||
      this.awaitingVerdict !== null ||
      document.visibilityState !== 'visible'
    )
      return;
    this.checkIn(this.state.info).catch(() => {});
  }

  /** The user chose "Later". */
  async deferUpdate() {
    const offer = this.state.update;
    const reader = this.state.info?.reader;
    if (!offer || !this.api || !reader) return;
    await usbDefer(this.api, offer.id, reader);
    this.deferred = offer.id;
    this.set({
      update: null,
      updateNote: {
        ok: true,
        text: `Firmware ${offer.version} will be offered again next time the scanner connects.`
      }
    });
  }

  /** Install the waiting update over the serial link. */
  async installUpdate() {
    const offer = this.state.update;
    const reader = this.state.info?.reader as string | undefined;
    const api = this.api;
    if (!offer || !api || !reader || !this.link?.isOpen || this.state.updating)
      return;
    if (this.state.jobActive) {
      this.set({
        updateNote: {
          ok: false,
          text: 'Finish the tag job first; then the update can run.'
        }
      });
      return;
    }
    const progress = (patch: Partial<UpdateProgress>) =>
      this.set({
        updating: {
          ...(this.state.updating ?? {
            version: offer.version,
            stage: 'starting',
            done: 0,
            total: offer.size
          }),
          ...patch
        }
      });
    // A job in progress, as far as the port is concerned: kept open if the tab is hidden,
    // and taps are not acted on.
    this.setJobActive(true);
    this.set({ updateNote: null });
    progress({ stage: 'starting' });
    let claimed = false;
    try {
      const go = await usbStart(api, offer.id, reader);
      claimed = true;
      progress({ stage: 'fetching' });
      await usbReport(api, go.id, { reader, state: 'downloading' });
      const image = await fetchImage(api, go.url);
      if (image.length !== go.size)
        throw Object.assign(new Error('the image is not the size expected'), {
          code: 'bad_image'
        });
      const digest = await sha256Hex(image);
      if (digest && digest !== go.sha256)
        throw Object.assign(new Error('the image is not the one expected'), {
          code: 'bad_image'
        });

      progress({ stage: 'sending', done: 0, total: image.length });
      const ask = async (cmd: ScannerMessage, ms = 10000) => {
        const rsp = await this.link!.request(cmd, ms);
        if (!rsp.ok)
          throw Object.assign(
            new Error(rsp.detail || rsp.error || `${cmd.cmd} refused`),
            { code: rsp.error || 'refused' }
          );
        return rsp;
      };
      await ask({
        cmd: 'ota_begin',
        id: go.id,
        size: image.length,
        sha256: go.sha256
      });
      let shown = 0;
      for (let at = 0; at < image.length; at += CHUNK) {
        const piece = image.subarray(at, at + CHUNK);
        await ask({
          cmd: 'ota_data',
          id: go.id,
          at,
          data: bytesToBase64(piece)
        });
        const done = at + piece.length;
        if (done - shown >= image.length / 100 || done === image.length) {
          shown = done;
          progress({ done });
        }
      }
      progress({ stage: 'checking' });
      await ask({ cmd: 'ota_end', id: go.id }, 30000);
      progress({ stage: 'restarting' });
      this.awaitingVerdict = go.id;
      await usbReport(api, go.id, { reader, state: 'restarting' }).catch(
        () => {}
      );
      // The scanner restarts now; the port comes back by itself (the `connect` event), and
      // the check-in then says how it went.
      this.set({
        updateNote: {
          ok: true,
          text: `Firmware ${offer.version} sent; the scanner is restarting into it and will reconnect by itself.`
        }
      });
    } catch (e: any) {
      const disconnected = !this.link?.isOpen;
      const code = disconnected ? 'interrupted' : e?.code || 'failed';
      if (claimed)
        await usbReport(api, offer.id, {
          reader,
          state: 'failed',
          error: code,
          detail: String(e?.message ?? '').slice(0, 200)
        }).catch(() => {});
      this.set({
        updateNote: {
          ok: false,
          text: `The firmware update did not finish: ${e?.message ?? e}`
        }
      });
      // A connection that is still there may be offered it again.
      if (this.state.info && this.link?.isOpen)
        this.checkIn(this.state.info).catch(() => {});
    } finally {
      this.set({ updating: null });
      this.setJobActive(false);
    }
  }

  dismissUpdateNote() {
    this.set({ updateNote: null });
  }

  /** Ask the user for the scanner (needs a click). */
  async chooseScanner(): Promise<void> {
    const port = await requestPort();
    await this.connect(port);
  }

  /** Take the port from another tab that holds it. */
  async takeOver(): Promise<void> {
    const port = await grantedPort();
    if (port) await this.connect(port, true);
  }

  /** Send a command and wait for its answer. */
  request(cmd: ScannerMessage, timeoutMs?: number): Promise<ScannerMessage> {
    if (!this.link?.isOpen)
      return Promise.reject(new Error('scanner not connected'));
    return this.link.request(cmd, timeoutMs);
  }

  send(cmd: ScannerMessage): Promise<void> {
    if (!this.link?.isOpen)
      return Promise.reject(new Error('scanner not connected'));
    return this.link.send(cmd);
  }

  /** A panel running a job: taps are reported to it rather than acted on here. */
  setJobActive(active: boolean) {
    this.set({ jobActive: active });
    if (!active && this.closeWhenIdle) {
      this.closeWhenIdle = false;
      if (document.visibilityState === 'hidden') this.link?.close();
    }
  }

  /** Everything the scanner says, for a panel following a job. */
  private messageListeners = new Set<(msg: ScannerMessage) => void>();

  onEachMessage(listener: (msg: ScannerMessage) => void) {
    this.messageListeners.add(listener);
    return () => {
      this.messageListeners.delete(listener);
    };
  }

  private onMessage(msg: ScannerMessage) {
    for (const l of this.messageListeners) l(msg);
    if (msg.evt === 'tag') {
      this.set({ onReader: msg });
      if (!this.state.jobActive) this.goToTaggedLocation(msg);
    } else if (msg.evt === 'tag_removed') {
      this.set({ onReader: null });
    }
  }

  /** A tap on a bin's tag opens that bin, unless it is the page already showing. */
  private goToTaggedLocation(msg: ScannerMessage) {
    const pk = locationFromUri(msg.uri);
    if (!pk || !this.navigate) return;
    const here = window.location.pathname.match(/\/stock\/location\/(\d+)\/?$/);
    if (here && Number(here[1]) === pk) return;
    this.navigate(`/stock/location/${pk}`);
  }
}

export const scanner = new ScannerService();

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
 * It holds the port only while the tab is visible, and in one tab at a time.
 */
import { type LinkState, type ScannerMessage, ScannerLink, grantedPort, hasWebSerial, locationFromUri, requestPort } from './serial';

type Navigate = (path: string) => void;

export interface ScannerState {
  link: LinkState;
  info: ScannerMessage | null;      // the scanner's `info`, once connected
  onReader: ScannerMessage | null;  // the tag on the reader, from `tag` events
  jobActive: boolean;               // a job is running from some panel: taps are its business
}

class ScannerService {
  private link: ScannerLink | null = null;
  private listeners = new Set<() => void>();
  private navigate: Navigate | null = null;
  private started = false;
  private connecting = false;

  state: ScannerState = { link: 'closed', info: null, onReader: null, jobActive: false };

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
  attach(navigate: Navigate) {
    this.navigate = navigate;
    if (this.started || !hasWebSerial()) return;
    this.started = true;
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'hidden') this.link?.close();
      else this.reconnect();
    });
    navigator.serial?.addEventListener('disconnect', () => this.link?.close());
    this.reconnect();
  }

  private ensureLink(): ScannerLink {
    if (!this.link) {
      this.link = new ScannerLink();
      this.link.onState = (link) => this.set({ link, ...(link === 'closed' ? { info: null, onReader: null } : {}) });
      this.link.onMessage = (msg) => this.onMessage(msg);
    }
    return this.link;
  }

  get isOpen(): boolean {
    return this.link?.isOpen ?? false;
  }

  /** Open a port the user granted earlier, if the tab is visible and nothing is open. */
  async reconnect() {
    if (document.visibilityState !== 'visible' || this.isOpen || this.connecting) return;
    const port = await grantedPort();
    if (port) await this.connect(port);
  }

  async connect(port: SerialPort, steal = false): Promise<void> {
    const link = this.ensureLink();
    if (link.isOpen || this.connecting) return;
    this.connecting = true;
    try {
      if (!(await link.open(port, steal))) return;
      await link.request({ cmd: 'hid', enabled: false }).catch(() => {});
      const info = await link.request({ cmd: 'info' });
      this.set({ info, onReader: info.tag ? { uid: info.tag } : null });
    } finally {
      this.connecting = false;
    }
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
    if (!this.link?.isOpen) return Promise.reject(new Error('scanner not connected'));
    return this.link.request(cmd, timeoutMs);
  }

  send(cmd: ScannerMessage): Promise<void> {
    if (!this.link?.isOpen) return Promise.reject(new Error('scanner not connected'));
    return this.link.send(cmd);
  }

  /** A panel running a job: taps are reported to it rather than acted on here. */
  setJobActive(active: boolean) {
    this.set({ jobActive: active });
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

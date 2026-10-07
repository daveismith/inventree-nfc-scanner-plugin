/**
 * The scanner over WebSerial: the same newline-delimited JSON protocol as tools/nfcprog.py
 * in the firmware repository.
 *
 * One tab holds the port at a time (the browser allows one open handle), coordinated with
 * the Web Locks API, and only while the tab is visible, so that a background tab does not
 * keep the scanner's keyboard output switched off all day.
 */

// Minimal Web Serial typings, so the build needs no extra package.
declare global {
  interface SerialPort {
    readable: ReadableStream<Uint8Array> | null;
    writable: WritableStream<Uint8Array> | null;
    open(options: { baudRate: number }): Promise<void>;
    close(): Promise<void>;
    setSignals(signals: { dataTerminalReady?: boolean }): Promise<void>;
    getInfo(): { usbVendorId?: number; usbProductId?: number };
  }
  interface Serial extends EventTarget {
    getPorts(): Promise<SerialPort[]>;
    requestPort(options?: {
      filters: { usbVendorId: number; usbProductId?: number }[];
    }): Promise<SerialPort>;
  }
  interface Navigator {
    serial?: Serial;
  }
}

export const USB_VENDOR = 0x303a;
export const USB_PRODUCT = 0x4e46;
export const LOCK_NAME = 'inventree-nfc-scanner-port';

export type ScannerMessage = Record<string, any>;

export type LinkState = 'closed' | 'opening' | 'open' | 'busy-elsewhere';

export function hasWebSerial(): boolean {
  return typeof navigator !== 'undefined' && !!navigator.serial;
}

function isScanner(port: SerialPort): boolean {
  const info = port.getInfo();
  return info.usbVendorId === USB_VENDOR && info.usbProductId === USB_PRODUCT;
}

/** A port the user has already granted, if any. */
export async function grantedPort(): Promise<SerialPort | null> {
  if (!hasWebSerial()) return null;
  const ports = await navigator.serial!.getPorts();
  return ports.find(isScanner) ?? null;
}

/** Ask the user for the scanner (needs a click). */
export async function requestPort(): Promise<SerialPort> {
  return navigator.serial!.requestPort({
    filters: [{ usbVendorId: USB_VENDOR, usbProductId: USB_PRODUCT }]
  });
}

export class ScannerLink {
  private port: SerialPort | null = null;
  private writer: WritableStreamDefaultWriter<Uint8Array> | null = null;
  private reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
  private releaseLock: (() => void) | null = null;
  private waiters: {
    cmd: string;
    resolve: (m: ScannerMessage) => void;
    reject: (e: Error) => void;
    timer: number;
  }[] = [];
  /** Callers waiting for the link to close (a job in progress, say) are told so. */
  private closeListeners = new Set<() => void>();

  state: LinkState = 'closed';
  onMessage: (msg: ScannerMessage) => void = () => {};
  onState: (state: LinkState) => void = () => {};

  private setState(state: LinkState) {
    this.state = state;
    this.onState(state);
  }

  /**
   * Open the port, holding the tab lock for as long as it is open. With `steal`, take the
   * lock from another tab that holds it; that tab closes its port when it loses the lock.
   */
  private opening: Promise<boolean> | null = null;

  async open(port: SerialPort, steal = false): Promise<boolean> {
    if (this.port) return true;
    if (this.opening) return this.opening; // a second caller joins the first
    this.opening = this.doOpen(port, steal).finally(() => {
      this.opening = null;
    });
    return this.opening;
  }

  private async doOpen(port: SerialPort, steal: boolean): Promise<boolean> {
    this.setState('opening');

    const locks = (navigator as any).locks;
    if (locks?.request) {
      const got = await new Promise<boolean>((resolveGot) => {
        locks
          .request(
            LOCK_NAME,
            { ifAvailable: !steal, steal },
            (lock: unknown) => {
              if (!lock) {
                resolveGot(false);
                return;
              }
              resolveGot(true);
              // Held until release() is called, or the lock is stolen (the promise rejects).
              return new Promise<void>((release) => {
                this.releaseLock = release;
              });
            }
          )
          .catch(() => {
            // Stolen by another tab: let go of the port.
            this.releaseLock = null;
            this.close();
          });
      });
      if (!got) {
        this.setState('busy-elsewhere');
        return false;
      }
    }

    try {
      await port.open({ baudRate: 115200 });
      await port.setSignals({ dataTerminalReady: true });
    } catch (e) {
      this.releaseLock?.();
      this.releaseLock = null;
      this.setState('closed');
      throw e;
    }
    this.port = port;
    this.writer = port.writable!.getWriter();
    this.setState('open');
    this.readLoopDone = this.readLoop();
    return true;
  }

  private readLoopDone: Promise<void> | null = null;

  private closing: Promise<void> | null = null;

  async close(): Promise<void> {
    if (this.closing) return this.closing;
    this.closing = this.doClose().finally(() => {
      this.closing = null;
    });
    return this.closing;
  }

  private async doClose(): Promise<void> {
    if (this.opening) {
      await this.opening.catch(() => {}); // let it finish, then close what it opened
    }
    const port = this.port;
    this.port = null;
    // Whoever is waiting for an answer gets one now, not never.
    const waiters = this.waiters;
    this.waiters = [];
    for (const w of waiters) {
      window.clearTimeout(w.timer);
      w.reject(new Error('scanner disconnected'));
    }
    for (const l of this.closeListeners) l();
    try {
      // The port closes only once both streams are unlocked: cancel the read and wait for
      // readLoop to release its lock, release the writer, then close.
      if (this.reader) await this.reader.cancel().catch(() => {});
      await this.readLoopDone?.catch(() => {});
      this.writer?.releaseLock();
      this.writer = null;
      await port?.close();
    } catch {
      /* already gone */
    }
    this.releaseLock?.();
    this.releaseLock = null;
    this.setState('closed');
  }

  /** Be told when the link closes, for as long as the returned function is not called. */
  onClose(listener: () => void): () => void {
    this.closeListeners.add(listener);
    return () => {
      this.closeListeners.delete(listener);
    };
  }

  get isOpen(): boolean {
    return this.port !== null;
  }

  async send(obj: ScannerMessage): Promise<void> {
    if (!this.writer) throw new Error('scanner not connected');
    await this.writer.write(
      new TextEncoder().encode(`${JSON.stringify(obj)}\n`)
    );
  }

  /** Send a command and wait for its `rsp`. Events arriving meanwhile still go to onMessage. */
  request(obj: ScannerMessage, timeoutMs = 3000): Promise<ScannerMessage> {
    return new Promise((resolve, reject) => {
      const timer = window.setTimeout(() => {
        this.waiters = this.waiters.filter((w) => w.timer !== timer);
        reject(new Error(`no answer to ${obj.cmd}`));
      }, timeoutMs);
      this.waiters.push({ cmd: obj.cmd, resolve, reject, timer });
      this.send(obj).catch((e) => {
        window.clearTimeout(timer);
        this.waiters = this.waiters.filter((w) => w.timer !== timer);
        reject(e);
      });
    });
  }

  private dispatch(msg: ScannerMessage) {
    if (msg.rsp) {
      const i = this.waiters.findIndex((w) => w.cmd === msg.rsp);
      if (i >= 0) {
        const [w] = this.waiters.splice(i, 1);
        window.clearTimeout(w.timer);
        w.resolve(msg);
      }
    }
    this.onMessage(msg);
  }

  private async readLoop() {
    if (!this.port?.readable) return;
    const decoder = new TextDecoder();
    let pending = '';
    this.reader = this.port.readable.getReader();
    try {
      for (;;) {
        const { value, done } = await this.reader.read();
        if (done) break;
        pending += decoder.decode(value, { stream: true });
        for (
          let nl = pending.indexOf('\n');
          nl >= 0;
          nl = pending.indexOf('\n')
        ) {
          const line = pending.slice(0, nl).trim();
          pending = pending.slice(nl + 1);
          if (!line) continue;
          try {
            this.dispatch(JSON.parse(line));
          } catch {
            /* not JSON: ignore */
          }
        }
      }
    } catch {
      /* the port went away */
    } finally {
      this.reader?.releaseLock();
      this.reader = null;
      this.readLoopDone = null;
      if (this.port) this.close(); // the device went away: close the rest
    }
  }
}

/** The location pk a tag's URI points at, if it is one of this server's. */
export function locationFromUri(uri: string | undefined): number | null {
  if (!uri) return null;
  const m = uri.match(/\/web\/stock\/location\/(\d+)\/?$/);
  return m ? Number(m[1]) : null;
}

/*
 * A stand-in for WebSerial, installed before any page script runs (page.add_init_script).
 *
 * Chromium cannot be given a fake serial device, so the API itself is replaced, with what
 * frontend/src/serial.ts uses: navigator.serial with getPorts, requestPort and the connect and
 * disconnect events, and one port (the scanner's USB ids) with open, close, setSignals,
 * getInfo and its two streams. Bytes go to Python and back through functions the test exposes
 * (tests/browser/conftest.py): the scanner itself is a Python model (usb_scanner.py).
 *
 *   __serial_open()         the port was opened
 *   __serial_write(text)    the page wrote this; "restart" back if the scanner restarts
 *   __serial_read() → text  what the scanner has said since, or ""
 *   __serial_close()        the port was closed
 *
 * The test drives the device through window.__serialShim: unplug() and replug() (a restart
 * after a firmware update looks like both), and grant(), as if the user had chosen the port
 * on an earlier visit.
 */
(() => {
  const encoder = new TextEncoder();
  const decoder = new TextDecoder();
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const gone = () => new DOMException('The device has been lost.', 'NetworkError');

  // A port the user chose stays granted across reloads, as Chrome keeps it; per tab here.
  const GRANT_KEY = '__serialGranted';
  const isGranted = () => sessionStorage.getItem(GRANT_KEY) === '1';
  const setGranted = () => sessionStorage.setItem(GRANT_KEY, '1');

  class FakePort extends EventTarget {
    constructor() {
      super();
      this.present = true;
      this.opened = false;
      this.readable = null;
      this.writable = null;
    }

    getInfo() {
      return { usbVendorId: 0x303a, usbProductId: 0x4e46 };
    }

    async open(options) {
      if (!this.present) throw gone();
      if (this.opened) throw new DOMException('The port is already open.', 'InvalidStateError');
      if (!options || !options.baudRate) throw new TypeError('baudRate is required');
      this.opened = true;
      await window.__serial_open();
      const port = this;
      let cancelled = false;
      this.readable = new ReadableStream({
        async pull(controller) {
          for (;;) {
            if (!port.present) {
              controller.error(gone());
              return;
            }
            if (cancelled || !port.opened) {
              controller.close();
              return;
            }
            const text = await window.__serial_read();
            if (text) {
              controller.enqueue(encoder.encode(text));
              return;
            }
            await sleep(10);
          }
        },
        cancel() {
          cancelled = true;
        }
      });
      this.writable = new WritableStream({
        async write(chunk) {
          if (!port.present) throw gone();
          const after = await window.__serial_write(decoder.decode(chunk));
          if (after === 'restart') {
            // As the real one does: gone from USB for a moment, then back.
            setTimeout(() => {
              window.__serialShim.unplug();
              setTimeout(() => window.__serialShim.replug(), 300);
            }, 20);
          }
        }
      });
    }

    async setSignals() {
      if (!this.opened) throw new DOMException('The port is closed.', 'InvalidStateError');
    }

    async close() {
      if (!this.opened) throw new DOMException('The port is already closed.', 'InvalidStateError');
      this.opened = false;
      this.readable = null;
      this.writable = null;
      await window.__serial_close();
    }
  }

  const port = new FakePort();
  const serial = new EventTarget();

  // The specification fires connect and disconnect at the port, bubbling to navigator.serial;
  // listeners there compare the event's target with their port.
  function fire(type) {
    const event = new Event(type);
    Object.defineProperty(event, 'target', { get: () => port });
    serial.dispatchEvent(event);
  }

  serial.getPorts = async () => (isGranted() && port.present ? [port] : []);
  serial.requestPort = async (options) => {
    const wanted = (options && options.filters) || [];
    const info = port.getInfo();
    const matches =
      wanted.length === 0 ||
      wanted.some(
        (f) =>
          f.usbVendorId === info.usbVendorId &&
          (f.usbProductId === undefined || f.usbProductId === info.usbProductId)
      );
    if (!port.present || !matches) throw new DOMException('No port selected by the user.', 'NotFoundError');
    setGranted();
    return port;
  };
  Object.defineProperty(navigator, 'serial', { value: serial, configurable: true });

  window.__serialShim = {
    grant() {
      setGranted();
    },
    unplug() {
      if (!port.present) return;
      port.present = false;
      port.opened = false;
      fire('disconnect');
    },
    replug() {
      if (port.present) return;
      port.present = true;
      fire('connect');
    },
    get opened() {
      return port.opened;
    }
  };
})();

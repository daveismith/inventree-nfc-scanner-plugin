/**
 * The "NFC tag" panel on a stock location's page.
 *
 * One button, "Program tag", by either of two routes: a scanner on this computer over
 * WebSerial, or a network scanner the server drives. Both end with the tag's UID linked to
 * the location as its barcode. The USB connection lives in scanner.ts and outlives this panel.
 */

import {
  checkPluginVersion,
  type InvenTreePluginContext
} from '@inventreedb/ui';
import {
  Alert,
  Badge,
  Button,
  Group,
  Select,
  Stack,
  Table,
  Text
} from '@mantine/core';
import { notifications } from '@mantine/notifications';
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore
} from 'react';

import {
  cancelJob,
  createJob,
  getJob,
  getJobs,
  getScanners,
  getTag,
  type Job,
  linkBarcode,
  recordUsbJob,
  type Scanner,
  type TagPayload
} from './api';
import { scanner } from './scanner';
import { hasWebSerial, type ScannerMessage } from './serial';
import { UpdateNotice } from './UpdateNotice';

interface PanelContext {
  location: number | string;
  has_password: boolean;
  job_timeout_s: number;
  can_program: boolean;
}

type Stage = 'idle' | 'queued' | 'waiting' | 'writing' | 'done' | 'failed';

interface Progress {
  stage: Stage;
  text: string;
  uid?: string;
  error?: string;
  existing?: string;
}

const STATE_COLOR: Record<string, string> = {
  done: 'green',
  failed: 'red',
  cancelled: 'gray',
  waiting: 'yellow',
  writing: 'blue',
  queued: 'gray',
  sent: 'gray'
};

function describe(job: Job): string {
  switch (job.state) {
    case 'queued':
      return 'queued: not yet collected by the scanner';
    case 'sent':
      return 'sent to the scanner';
    case 'waiting':
      return 'present a tag to the scanner';
    case 'writing':
      return 'writing: hold the tag still';
    case 'done':
      return `done: ${job.uid}${job.protected ? ', protected' : ''}`;
    case 'failed':
      return `failed: ${job.error}${job.existing_text ? ` (the tag holds ${job.existing_text})` : ''}`;
    case 'cancelled':
      return 'cancelled';
  }
}

const report = (e: any) => {
  notifications.show({
    title: 'Scanner',
    message: e?.message ?? String(e),
    color: 'red'
  });
};

function NfcPanel({ context }: { context: InvenTreePluginContext }) {
  const ctx = (context.context ?? {}) as PanelContext;
  // InvenTree hands the target id over as a string.
  const location = Number(ctx.location);
  const api = context.api;

  // --- USB route: the connection is the service's; this panel only watches it
  const usb = useSyncExternalStore(scanner.subscribe, scanner.getState);
  useEffect(() => {
    scanner.attach(context.navigate, context.api);
  }, [context.navigate, context.api]);

  // --- network route
  const [scanners, setScanners] = useState<Scanner[]>([]);
  const [scannerId, setScannerId] = useState<string | null>(null);
  const [netJob, setNetJob] = useState<Job | null>(null);

  // --- shared
  const [progress, setProgress] = useState<Progress>({
    stage: 'idle',
    text: ''
  });
  const [history, setHistory] = useState<Job[]>([]);
  const busy =
    progress.stage === 'queued' ||
    progress.stage === 'waiting' ||
    progress.stage === 'writing';
  const lastRoute = useRef<'usb' | 'net'>('usb');

  const refreshHistory = useCallback(() => {
    getJobs(api, location)
      .then(setHistory)
      .catch(() => {});
  }, [api, location]);

  useEffect(() => {
    refreshHistory();
    getScanners(api)
      .then((list) => {
        setScanners(list);
        const remembered = localStorage.getItem('nfc-scanner');
        const first =
          list.find((s) => s.id === remembered) ??
          list.find((s) => s.online) ??
          list[0];
        if (first) setScannerId(first.id);
      })
      .catch(() => {});
  }, [api, refreshHistory]);

  // Warn before leaving mid-job.
  useEffect(() => {
    if (!busy) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
    };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [busy]);

  const finishUsb = useCallback(
    async (final: ScannerMessage, overwrite: boolean) => {
      const done = final.evt === 'done';
      let linkFailure = '';
      if (done) {
        try {
          const { outcome } = await linkBarcode(api, location, final.uid);
          setProgress({
            stage: 'done',
            text: `done: ${final.uid}${final.protected ? ', protected' : ''}; UID ${outcome}`,
            uid: final.uid
          });
        } catch (e: any) {
          const detail = e?.response?.data;
          linkFailure = detail
            ? (detail.detail ?? detail.uid ?? JSON.stringify(detail))
            : e.message;
          setProgress({
            stage: 'failed',
            text: `tag written (${final.uid}), but the barcode link failed: ${linkFailure}`
          });
        }
      } else {
        setProgress({
          stage: 'failed',
          text: `failed: ${final.error}`,
          error: final.error,
          existing: final.text
        });
      }
      // The record says what happened to the bin: a tag written but not linked is a failure.
      recordUsbJob(api, location, {
        state: done
          ? linkFailure
            ? 'failed'
            : 'done'
          : final.error === 'cancelled'
            ? 'cancelled'
            : 'failed',
        overwrite,
        uid: String(final.uid ?? '').slice(0, 20),
        tag_type: String(final.type ?? '').slice(0, 12),
        protected:
          typeof final.protected === 'boolean' ? final.protected : null,
        error: (linkFailure ? 'link_failed' : String(final.error ?? '')).slice(
          0,
          32
        ),
        error_detail: (linkFailure
          ? `tag written; barcode link failed: ${linkFailure}`
          : String(final.detail ?? '')
        ).slice(0, 200)
      })
        .then(refreshHistory)
        .catch(() => {});
    },
    [api, location, refreshHistory]
  );

  /** An attempt that never reached the scanner, or was refused by it, is recorded too. */
  const recordAttempt = useCallback(
    (overwrite: boolean, error: string, detail: string) => {
      recordUsbJob(api, location, {
        state: 'failed',
        overwrite,
        error: error.slice(0, 32),
        error_detail: detail.slice(0, 200)
      })
        .then(refreshHistory)
        .catch(() => {});
    },
    [api, location, refreshHistory]
  );

  const programUsb = useCallback(
    async (overwrite: boolean) => {
      if (!scanner.isOpen) return;
      netJobId.current = null; // a network job's late answers are not this job's
      netJobEnded.current = false;
      // From here on taps belong to this job, including one during the fetch.
      scanner.setJobActive(true);
      setProgress({ stage: 'queued', text: 'fetching the tag data' });
      let tag: TagPayload;
      try {
        tag = await getTag(api, location);
      } catch (e: any) {
        const why = e?.response?.data?.base_url ?? e.message;
        setProgress({ stage: 'failed', text: why });
        recordAttempt(overwrite, 'no_tag_data', why);
        scanner.setJobActive(false);
        return;
      }
      const id = Date.now() % 1000000;
      const cmd: ScannerMessage = {
        cmd: 'program',
        id,
        ndef: tag.ndef,
        timeout_ms: tag.timeout_s * 1000
      };
      if (overwrite) cmd.overwrite = true;
      if (tag.pwd) {
        cmd.pwd = tag.pwd;
        cmd.pack = tag.pack;
      }

      let unsubscribe = () => {};
      let unsubscribeClose = () => {};
      let timer = 0;
      // Settles on the job's result; or when the scanner goes away (the tab hidden, the
      // device unplugged, another tab taking over); or when the scanner's own timeout has
      // passed with no word, which should not happen but must not hang the panel.
      const final = new Promise<ScannerMessage>((resolve, reject) => {
        unsubscribe = scanner.onEachMessage((msg) => {
          if (msg.id !== id) return;
          if (msg.evt === 'waiting')
            setProgress({
              stage: 'waiting',
              text: 'present the tag to the scanner'
            });
          if (msg.evt === 'writing')
            setProgress({
              stage: 'writing',
              text: `writing ${msg.uid}: hold still`
            });
          if (msg.evt === 'done' || msg.evt === 'failed') resolve(msg);
        });
        unsubscribeClose = scanner.onClose(() =>
          reject(new Error('the scanner disconnected during the job'))
        );
        timer = window.setTimeout(
          () =>
            reject(
              new Error('no word from the scanner within the job timeout')
            ),
          tag.timeout_s * 1000 + 5000
        );
      });
      final.catch(() => {}); // awaited below; never an unhandled rejection meanwhile
      try {
        const rsp = await scanner.request(cmd);
        if (!rsp.ok) {
          setProgress({
            stage: 'failed',
            text: `refused: ${rsp.error}`,
            error: rsp.error
          });
          recordAttempt(overwrite, rsp.error ?? 'refused', rsp.detail ?? '');
          return;
        }
        await finishUsb(await final, overwrite);
      } catch (e: any) {
        setProgress({ stage: 'failed', text: e.message });
        const disconnected = !scanner.isOpen || /disconnected/.test(e.message);
        recordAttempt(
          overwrite,
          disconnected ? 'no_scanner' : 'no_answer',
          e.message
        );
      } finally {
        window.clearTimeout(timer);
        unsubscribe();
        unsubscribeClose();
        scanner.setJobActive(false);
      }
    },
    [api, location, finishUsb]
  );

  const cancelUsb = useCallback(() => {
    scanner.send({ cmd: 'cancel' }).catch(() => {});
  }, []);

  // --- network route

  const programNet = useCallback(
    async (overwrite: boolean) => {
      if (!scannerId) return;
      localStorage.setItem('nfc-scanner', scannerId);
      try {
        const job = await createJob(api, {
          location,
          scanner: scannerId,
          overwrite
        });
        netJobId.current = job.id;
        netJobEnded.current = false;
        setNetJob(job);
        setProgress({ stage: 'queued', text: describe(job) });
      } catch (e: any) {
        const data = e?.response?.data;
        setProgress({
          stage: 'failed',
          text: data ? JSON.stringify(data) : e.message
        });
      }
    },
    [api, location, scannerId]
  );

  // The network job being followed, by id, for answers that arrive after it changed.
  const netJobId = useRef<number | null>(null);
  const netJobEnded = useRef(false);

  /** A network job as the server now reports it: shown, and ended when it has ended. */
  const applyJob = useCallback(
    (job: Job) => {
      if (netJobId.current !== null && job.id !== netJobId.current) return; // a stale poll
      // An answer that was in flight when the job ended must not bring it back.
      const ended = ['done', 'failed', 'cancelled'].includes(job.state);
      if (netJobEnded.current && !ended) return;
      netJobEnded.current = ended;
      setNetJob(job);
      const stage: Stage =
        job.state === 'done'
          ? 'done'
          : job.state === 'failed' || job.state === 'cancelled'
            ? 'failed'
            : job.state === 'queued' || job.state === 'sent'
              ? 'queued'
              : job.state;
      setProgress({
        stage,
        text: describe(job),
        uid: job.uid,
        error: job.error,
        existing: job.existing_text
      });
      if (['done', 'failed', 'cancelled'].includes(job.state)) refreshHistory();
    },
    [refreshHistory]
  );

  useEffect(() => {
    if (!netJob || ['done', 'failed', 'cancelled'].includes(netJob.state))
      return;
    const timer = window.setInterval(async () => {
      try {
        applyJob(await getJob(api, netJob.id));
      } catch {
        /* keep polling */
      }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [api, netJob, refreshHistory]);

  const cancelNet = useCallback(() => {
    // A job cancelled at once comes back already ended; applyJob settles the panel for it.
    if (netJob)
      cancelJob(api, netJob.id)
        .then(applyJob)
        .catch(() => {});
  }, [api, netJob, applyJob]);

  // --- rendering

  const usbReady = usb.link === 'open' && !usb.updating;
  const canOverwrite =
    progress.stage === 'failed' && progress.error === 'not_blank';

  const start = (overwrite: boolean, route: 'usb' | 'net') => {
    lastRoute.current = route;
    if (route === 'usb' && !scanner.isOpen) {
      setProgress({ stage: 'failed', text: 'the scanner is not connected' });
      return Promise.resolve();
    }
    return route === 'usb' ? programUsb(overwrite) : programNet(overwrite);
  };

  const scannerOptions = useMemo(
    () =>
      scanners.map((s) => ({
        value: s.id,
        label: `${s.name}${s.online ? '' : ' (offline)'}`
      })),
    [scanners]
  );

  if (!ctx.can_program) {
    return (
      <Text c='dimmed'>
        You need permission to change stock locations to program tags.
      </Text>
    );
  }

  return (
    <Stack gap='md'>
      {/* USB */}
      <Stack gap='xs'>
        <Group justify='space-between'>
          <Text fw={600}>Scanner on this computer</Text>
          {!hasWebSerial() ? (
            <Badge color='gray'>this browser has no WebSerial</Badge>
          ) : usb.link === 'open' ? (
            <Badge color='green'>
              connected{usb.info?.pn532 ? '' : ', no reader'}
            </Badge>
          ) : usb.link === 'busy-elsewhere' ? (
            <Badge color='yellow'>in use by another tab or window</Badge>
          ) : usb.link === 'opening' ? (
            <Badge color='gray'>connecting</Badge>
          ) : (
            <Badge color='gray'>not connected</Badge>
          )}
        </Group>
        <UpdateNotice />
        {hasWebSerial() && (
          <Group>
            {usb.link === 'closed' && (
              <Button
                variant='default'
                onClick={() => scanner.chooseScanner().catch(() => {})}
              >
                Connect scanner
              </Button>
            )}
            {usb.link === 'busy-elsewhere' && (
              <Button
                variant='default'
                onClick={() => scanner.takeOver().catch(report)}
              >
                Take over
              </Button>
            )}
            {usbReady && (
              <Button onClick={() => start(false, 'usb')} disabled={busy}>
                Program tag
              </Button>
            )}
            {usbReady &&
              lastRoute.current === 'usb' &&
              (progress.stage === 'waiting' ||
                progress.stage === 'writing') && (
                <Button variant='subtle' color='red' onClick={cancelUsb}>
                  Cancel
                </Button>
              )}
          </Group>
        )}
        {usbReady && usb.onReader && (
          <Text size='sm' c='dimmed'>
            On the reader: {usb.onReader.uid}
            {usb.onReader.text
              ? ` holding ${usb.onReader.text}`
              : usb.onReader.type
                ? ` (${usb.onReader.type}, blank)`
                : ''}
            {usb.onReader.protected ? ', protected' : ''}
          </Text>
        )}
      </Stack>

      {/* Network */}
      <Stack gap='xs'>
        <Text fw={600}>Scanner on the network</Text>
        {scanners.length === 0 ? (
          <Text size='sm' c='dimmed'>
            No network scanners are configured (Admin Center → Machines).
          </Text>
        ) : (
          <Group align='end'>
            <Select
              data={scannerOptions}
              value={scannerId}
              onChange={setScannerId}
              label='Scanner'
              w={260}
            />
            <Button
              onClick={() => start(false, 'net')}
              disabled={busy || !scannerId}
            >
              Program tag
            </Button>
            {busy && lastRoute.current === 'net' && (
              <Button variant='subtle' color='red' onClick={cancelNet}>
                Cancel
              </Button>
            )}
          </Group>
        )}
      </Stack>

      {/* Progress */}
      {progress.stage !== 'idle' && (
        <Alert
          color={
            progress.stage === 'done'
              ? 'green'
              : progress.stage === 'failed'
                ? 'red'
                : 'blue'
          }
          title={
            progress.stage === 'done'
              ? 'Programmed'
              : progress.stage === 'failed'
                ? 'Not programmed'
                : 'Programming'
          }
        >
          <Stack gap='xs'>
            <Text>{progress.text}</Text>
            {canOverwrite && (
              <Group>
                <Text size='sm'>
                  The tag already holds a message. Replace it?
                </Text>
                <Button
                  size='xs'
                  color='orange'
                  onClick={() => start(true, lastRoute.current)}
                >
                  Overwrite
                </Button>
              </Group>
            )}
          </Stack>
        </Alert>
      )}

      {ctx.has_password ? null : (
        <Text size='xs' c='dimmed'>
          No tag password is set, so tags are left unprotected (plugin settings
          → Tag password).
        </Text>
      )}

      {/* History */}
      {history.length > 0 && (
        <Table withTableBorder={false} verticalSpacing='xs' fz='sm'>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>When</Table.Th>
              <Table.Th>Who</Table.Th>
              <Table.Th>Via</Table.Th>
              <Table.Th>Result</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {history.slice(0, 8).map((job) => (
              <Table.Tr key={job.id}>
                <Table.Td>{new Date(job.created_at).toLocaleString()}</Table.Td>
                <Table.Td>{job.created_by_name ?? ''}</Table.Td>
                <Table.Td>{job.scanner?.name ?? 'USB'}</Table.Td>
                <Table.Td>
                  <Badge
                    color={STATE_COLOR[job.state] ?? 'gray'}
                    variant='light'
                  >
                    {job.state}
                  </Badge>{' '}
                  {job.uid || job.error}
                  {job.error_detail && (
                    <Text c='dimmed' size='xs' component='span'>
                      {' '}
                      {job.error_detail}
                    </Text>
                  )}
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
    </Stack>
  );
}

export function RenderNfcPanel(context: InvenTreePluginContext) {
  checkPluginVersion(context);
  return <NfcPanel context={context} />;
}

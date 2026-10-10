/**
 * A firmware update for the USB scanner, as one of InvenTree's notifications: the offer (with
 * "Update now" and "Later"), its progress, and how it went. InvenTree shows its notifications
 * in a corner of every page and keeps them as the user moves around, so the update is seen
 * wherever they are, not only on the dashboard item or a location's panel. The work is the
 * scanner service's; this keeps one notification in step with its state.
 *
 * Started by the panel and the dashboard item as they mount (startUpdateOverlay), it stays
 * when they unmount: it lives as long as the service, the rest of the page load.
 */
import { Button, Group, Progress, Text } from '@mantine/core';
import { notifications } from '@mantine/notifications';

import { type ScannerState, scanner } from './scanner';

const ID = 'inventree-nfc-scanner-update';

const STAGE: Record<string, string> = {
  starting: 'Starting',
  fetching: 'Fetching the firmware from the server',
  sending: 'Sending it to the scanner',
  checking: 'The scanner is checking it',
  restarting: 'Restarting'
};

let started = false;
let shown = ''; // what the notification shows now, so it is only redrawn on a change

export function startUpdateOverlay() {
  if (started) return;
  started = true;
  scanner.subscribe(() => sync(scanner.getState()));
  sync(scanner.getState());
}

function percent(s: ScannerState): number {
  const u = s.updating;
  return u && u.total > 0 ? Math.round((u.done * 100) / u.total) : 0;
}

/** What to show for this state, and a key that changes when it should be redrawn. */
function view(s: ScannerState) {
  if (s.updating) {
    const pct = percent(s);
    const sending = s.updating.stage === 'sending';
    return {
      key: `updating:${s.updating.version}:${s.updating.stage}:${pct}`,
      props: {
        title: `Updating the scanner to ${s.updating.version}`,
        color: 'blue',
        autoClose: false as const,
        withCloseButton: false,
        message: (
          <>
            <Text size='sm'>
              {STAGE[s.updating.stage]}
              {sending ? ` (${pct}%)` : ''}. Leave it plugged in and this page
              open.
            </Text>
            <Progress mt='xs' value={sending ? pct : 100} animated={!sending} />
          </>
        )
      }
    };
  }
  if (s.update && s.link === 'open') {
    const u = s.update;
    return {
      key: `offer:${u.id}:${u.required}:${s.jobActive}`,
      props: {
        title: `Firmware ${u.version} for this scanner`,
        color: u.required ? 'orange' : 'blue',
        autoClose: false as const,
        withCloseButton: false,
        message: (
          <>
            <Text size='sm'>
              {u.required
                ? 'An administrator requires this update; it installs now.'
                : `An administrator has made it available${
                    u.required_after
                      ? `; from ${new Date(u.required_after).toLocaleString()} it is required`
                      : ''
                  }. It takes about half a minute.`}
            </Text>
            {!u.required && (
              <Group mt='xs'>
                <Button
                  size='xs'
                  onClick={() => scanner.installUpdate().catch(() => {})}
                  disabled={s.jobActive}
                >
                  Update now
                </Button>
                <Button
                  size='xs'
                  variant='default'
                  onClick={() => scanner.deferUpdate().catch(() => {})}
                >
                  Later
                </Button>
              </Group>
            )}
          </>
        )
      }
    };
  }
  if (s.updateNote) {
    return {
      key: `note:${s.updateNote.ok}:${s.updateNote.text}`,
      props: {
        title: s.updateNote.ok ? 'Scanner firmware' : 'Scanner firmware update',
        color: s.updateNote.ok ? 'green' : 'red',
        // A success goes by itself; a failure stays until it is read.
        autoClose: s.updateNote.ok ? 10000 : (false as const),
        withCloseButton: true,
        onClose: () => scanner.dismissUpdateNote(),
        message: <Text size='sm'>{s.updateNote.text}</Text>
      }
    };
  }
  return null;
}

function sync(s: ScannerState) {
  const v = view(s);
  const key = v?.key ?? '';
  if (key === shown) return;
  const wasShown = shown !== '';
  shown = key;
  if (!v) {
    notifications.hide(ID);
    return;
  }
  if (wasShown) notifications.update({ id: ID, ...v.props });
  else notifications.show({ id: ID, ...v.props });
}

/**
 * A firmware update for the USB scanner: offered, in progress, or how it went. Shown by the
 * location panel and the dashboard item; the work is the scanner service's.
 */
import { Alert, Button, Group, Progress, Text } from '@mantine/core';
import { useSyncExternalStore } from 'react';

import { scanner } from './scanner';

const STAGE: Record<string, string> = {
  starting: 'Starting',
  fetching: 'Fetching the firmware from the server',
  sending: 'Sending it to the scanner',
  checking: 'The scanner is checking it',
  restarting: 'Restarting'
};

export function UpdateNotice() {
  const s = useSyncExternalStore(scanner.subscribe, scanner.getState);

  if (s.updating) {
    const pct =
      s.updating.total > 0
        ? Math.round((s.updating.done * 100) / s.updating.total)
        : 0;
    return (
      <Alert
        color='blue'
        title={`Updating the scanner to ${s.updating.version}`}
      >
        <Text size='sm'>
          {STAGE[s.updating.stage]}
          {s.updating.stage === 'sending' ? ` (${pct}%)` : ''}. Leave it plugged
          in and this page open.
        </Text>
        <Progress
          mt='xs'
          value={s.updating.stage === 'sending' ? pct : 100}
          animated={s.updating.stage !== 'sending'}
        />
      </Alert>
    );
  }

  return (
    <>
      {s.update && s.link === 'open' && (
        <Alert
          color={s.update.required ? 'orange' : 'blue'}
          title={`Firmware ${s.update.version} for this scanner`}
        >
          <Text size='sm'>
            {s.update.required
              ? 'An administrator requires this update; it installs now.'
              : `An administrator has made it available${
                  s.update.required_after
                    ? `; from ${new Date(s.update.required_after).toLocaleString()} it is required`
                    : ''
                }. It takes about half a minute.`}
          </Text>
          {!s.update.required && (
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
        </Alert>
      )}
      {s.updateNote && (
        <Alert
          color={s.updateNote.ok ? 'green' : 'red'}
          withCloseButton
          onClose={() => scanner.dismissUpdateNote()}
        >
          <Text size='sm'>{s.updateNote.text}</Text>
        </Alert>
      )}
    </>
  );
}

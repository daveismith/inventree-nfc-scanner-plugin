/**
 * Dashboard item: the scanner on this computer, and the network scanners with what they last
 * saw. Being on the dashboard, it also brings the USB connection up as soon as the user lands
 * in InvenTree, so a tap opens a bin's page from anywhere in the app after that.
 */

import {
  checkPluginVersion,
  type InvenTreePluginContext
} from '@inventreedb/ui';
import { Badge, Button, Group, Stack, Table, Text } from '@mantine/core';
import { useEffect, useState, useSyncExternalStore } from 'react';

import { getScanners, type Scanner } from './api';
import { scanner } from './scanner';
import { hasWebSerial } from './serial';
import { UpdateNotice } from './UpdateNotice';

function ago(iso: string | null): string {
  if (!iso) return 'never';
  const s = Math.max(
    0,
    Math.round((Date.now() - new Date(iso).getTime()) / 1000)
  );
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

function NfcDashboardItem({ context }: { context: InvenTreePluginContext }) {
  const [scanners, setScanners] = useState<Scanner[]>([]);
  const [usbError, setUsbError] = useState<string | null>(null);
  const usb = useSyncExternalStore(scanner.subscribe, scanner.getState);

  useEffect(() => {
    scanner.attach(context.navigate, context.api);
  }, [context.navigate, context.api]);

  useEffect(() => {
    const load = () =>
      getScanners(context.api)
        .then(setScanners)
        .catch(() => {});
    load();
    const timer = window.setInterval(load, 10000);
    return () => window.clearInterval(timer);
  }, [context.api]);

  return (
    <Stack gap='xs'>
      {hasWebSerial() && (
        <Group gap='xs'>
          <Text size='sm'>This computer:</Text>
          {usb.link === 'open' ? (
            <Badge color='green' variant='light'>
              scanner connected
              {usb.onReader?.text ? `, reading ${usb.onReader.text}` : ''}
            </Badge>
          ) : usb.link === 'busy-elsewhere' ? (
            <Button
              size='compact-xs'
              variant='default'
              onClick={() =>
                scanner.takeOver().then(
                  () => setUsbError(null),
                  (e) => setUsbError(e.message)
                )
              }
            >
              in use by another tab: take over
            </Button>
          ) : usb.link === 'opening' ? (
            <Text size='sm' c='dimmed'>
              connecting…
            </Text>
          ) : (
            <Button
              size='compact-xs'
              variant='default'
              onClick={() =>
                scanner.chooseScanner().then(
                  () => setUsbError(null),
                  (e) => setUsbError(e.message)
                )
              }
            >
              connect a scanner
            </Button>
          )}
          {usbError && (
            <Text c='red' size='sm'>
              {usbError}
            </Text>
          )}
        </Group>
      )}
      {hasWebSerial() && <UpdateNotice />}
      {scanners.length === 0 ? (
        <Text c='dimmed' size='sm'>
          No network NFC scanners are configured.
        </Text>
      ) : (
        <Table fz='sm' verticalSpacing='xs'>
          <Table.Tbody>
            {scanners.map((s) => (
              <Table.Tr key={s.id}>
                <Table.Td>
                  {s.name}
                  {s.warning && (
                    <Text c='orange' size='xs'>
                      {s.warning}
                    </Text>
                  )}
                </Table.Td>
                <Table.Td>
                  <Group gap={4} wrap='nowrap'>
                    {/* Its network link, as the server sees it. */}
                    <Badge color={s.online ? 'green' : 'red'} variant='light'>
                      {s.status}
                    </Badge>
                    {/* And whether it is the one plugged in here, which is a separate route. */}
                    {usb.link === 'open' &&
                      s.reader &&
                      usb.info?.reader === s.reader && (
                        <Badge color='green' variant='outline'>
                          USB here
                        </Badge>
                      )}
                  </Group>
                </Table.Td>
                <Table.Td>{ago(s.last_seen)}</Table.Td>
                <Table.Td>
                  {s.last_tag
                    ? `last tap: ${s.last_tag.text ?? s.last_tag.uid ?? s.last_tag.error}`
                    : ''}
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
    </Stack>
  );
}

export function RenderNfcDashboardItem(context: InvenTreePluginContext) {
  checkPluginVersion(context);
  return <NfcDashboardItem context={context} />;
}

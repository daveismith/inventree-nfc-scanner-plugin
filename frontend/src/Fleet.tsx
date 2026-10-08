/**
 * Dashboard item for admins: the firmware every scanner runs, the releases the server holds,
 * and deploying one to some or all scanners. A network scanner takes it on its next call; a
 * USB scanner when a browser next connects to it. See docs/fleet-updates.md.
 */

import {
  checkPluginVersion,
  type InvenTreePluginContext
} from '@inventreedb/ui';
import {
  Alert,
  Anchor,
  Badge,
  Button,
  Checkbox,
  FileInput,
  Group,
  Select,
  Stack,
  Table,
  Tabs,
  Text,
  TextInput
} from '@mantine/core';
import { useCallback, useEffect, useMemo, useState } from 'react';

import {
  cancelDeployment,
  checkReleases,
  deleteFirmware,
  deployFirmware,
  type Fleet,
  type FleetDeployment,
  forgetScanner,
  getDeployments,
  getFleet,
  uploadRelease
} from './api';

const STATE_COLOR: Record<string, string> = {
  pending: 'gray',
  sent: 'blue',
  downloading: 'blue',
  restarting: 'blue',
  confirmed: 'green',
  failed: 'red',
  rolled_back: 'orange',
  superseded: 'gray',
  cancelled: 'gray'
};

function ago(iso: string | null): string {
  if (!iso) return 'never';
  const s = Math.max(
    0,
    Math.round((Date.now() - new Date(iso).getTime()) / 1000)
  );
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

function errorText(e: any): string {
  const d = e?.response?.data;
  if (d && typeof d === 'object')
    return Object.values(d).flat().map(String).join(' ');
  return String(e?.message ?? e);
}

function DeploymentBadge({ d }: { d: FleetDeployment }) {
  return (
    <Stack gap={0}>
      <Group gap={4}>
        <Badge color={STATE_COLOR[d.state] ?? 'gray'} variant='light'>
          {d.state.replace('_', ' ')}
        </Badge>
        <Text size='xs'>
          {d.version}
          {d.via ? ` over ${d.via === 'usb' ? 'USB' : 'the network'}` : ''}
        </Text>
      </Group>
      {d.state === 'pending' && (
        <Text size='xs' c='dimmed'>
          {d.required ? 'required' : 'deferrable'}
          {d.deferrals ? `, put off ${d.deferrals}×` : ''}
          {d.error ? `, last attempt: ${d.error}` : ''}
        </Text>
      )}
      {d.detail && d.state !== 'pending' && (
        <Text size='xs' c='dimmed'>
          {d.error ? `${d.error}: ` : ''}
          {d.detail}
        </Text>
      )}
    </Stack>
  );
}

function FleetItem({ context }: { context: InvenTreePluginContext }) {
  const api = context.api;
  const [fleet, setFleet] = useState<Fleet | null>(null);
  const [history, setHistory] = useState<FleetDeployment[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Deploy form
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [firmwareId, setFirmwareId] = useState<string | null>(null);
  const [required, setRequired] = useState<string>('policy');
  const [requiredAfter, setRequiredAfter] = useState('');
  const [downgrade, setDowngrade] = useState(false);

  // Upload form
  const [files, setFiles] = useState<Record<string, File | null>>({
    manifest: null,
    app: null,
    merged: null
  });

  const load = useCallback(() => {
    getFleet(api)
      .then((f) => {
        setFleet(f);
        setError(null);
      })
      .catch((e) => setError(errorText(e)));
    getDeployments(api)
      .then(setHistory)
      .catch(() => {});
  }, [api]);

  useEffect(() => {
    load();
    const timer = window.setInterval(load, 5000);
    return () => window.clearInterval(timer);
  }, [load]);

  const deployable = useMemo(
    () =>
      (fleet?.firmware ?? [])
        .filter((f) => f.available && !f.incompatible)
        .map((f) => ({
          value: String(f.id),
          label: `${f.version}${f.prerelease ? ' (pre-release)' : ''}`
        })),
    [fleet]
  );

  const act = async (what: () => Promise<any>, done?: (r: any) => void) => {
    setBusy(true);
    setNote(null);
    try {
      const r = await what();
      done?.(r);
      setError(null);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
      load();
    }
  };

  const deploy = (all: boolean) =>
    act(
      () =>
        deployFirmware(api, {
          firmware: Number(firmwareId),
          scanners: all ? 'all' : Array.from(chosen),
          required: required === 'policy' ? null : required === 'required',
          required_after: requiredAfter
            ? new Date(requiredAfter).toISOString()
            : null,
          allow_downgrade: downgrade
        }),
      (r) => {
        const made = r.results.filter((x: any) => x.deployment).length;
        const refused = r.results.filter((x: any) => x.refused);
        setNote(
          `${made} deployment${made === 1 ? '' : 's'} made.` +
            (refused.length
              ? ` Not deployed: ${refused.map((x: any) => `${x.reader} (${x.refused})`).join('; ')}`
              : '')
        );
        setChosen(new Set());
      }
    );

  if (!fleet)
    return error ? <Text c='red'>{error}</Text> : <Text>Loading…</Text>;

  const toggle = (reader: string) => {
    const next = new Set(chosen);
    if (next.has(reader)) next.delete(reader);
    else next.add(reader);
    setChosen(next);
  };

  return (
    <Stack gap='xs'>
      {error && (
        <Alert color='red' withCloseButton onClose={() => setError(null)}>
          {error}
        </Alert>
      )}
      {note && (
        <Alert color='blue' withCloseButton onClose={() => setNote(null)}>
          {note}
        </Alert>
      )}
      <Tabs defaultValue='scanners'>
        <Tabs.List>
          <Tabs.Tab value='scanners'>Scanners</Tabs.Tab>
          <Tabs.Tab value='releases'>Releases</Tabs.Tab>
          <Tabs.Tab value='history'>History</Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value='scanners' pt='xs'>
          <Stack gap='xs'>
            {fleet.scanners.length === 0 ? (
              <Text c='dimmed' size='sm'>
                No scanner has been heard from yet. Network scanners appear when
                they call; USB scanners when a browser connects to one.
              </Text>
            ) : (
              <Table fz='sm' verticalSpacing={4}>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th />
                    <Table.Th>Scanner</Table.Th>
                    <Table.Th>Firmware</Table.Th>
                    <Table.Th>Last seen</Table.Th>
                    <Table.Th>Update</Table.Th>
                    <Table.Th />
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {fleet.scanners.map((s) => (
                    <Table.Tr key={s.reader}>
                      <Table.Td>
                        <Checkbox
                          checked={chosen.has(s.reader)}
                          onChange={() => toggle(s.reader)}
                          aria-label={`choose ${s.reader}`}
                        />
                      </Table.Td>
                      <Table.Td>
                        <Text size='sm'>{s.machine?.name ?? s.reader}</Text>
                        {s.machine && (
                          <Text size='xs' c='dimmed'>
                            {s.reader}
                          </Text>
                        )}
                      </Table.Td>
                      <Table.Td>
                        <Group gap={4}>
                          <Text size='sm'>{s.fw ?? 'unknown'}</Text>
                          {s.outdated && (
                            <Badge size='xs' color='yellow' variant='light'>
                              {fleet.newest} available
                            </Badge>
                          )}
                        </Group>
                      </Table.Td>
                      <Table.Td>
                        <Text size='sm'>{ago(s.last_seen)}</Text>
                        <Text size='xs' c='dimmed'>
                          {s.last_via === 'usb'
                            ? `USB${s.last_user ? `, ${s.last_user}` : ''}`
                            : (s.last_via ?? '')}
                        </Text>
                      </Table.Td>
                      <Table.Td>
                        {s.deployment ? (
                          <DeploymentBadge d={s.deployment} />
                        ) : null}
                      </Table.Td>
                      <Table.Td>
                        <Group gap={4} wrap='nowrap'>
                          {s.deployment?.state === 'pending' && (
                            <Button
                              size='compact-xs'
                              variant='subtle'
                              disabled={busy}
                              onClick={() =>
                                act(() =>
                                  cancelDeployment(api, s.deployment!.id)
                                )
                              }
                            >
                              withdraw
                            </Button>
                          )}
                          <Button
                            size='compact-xs'
                            variant='subtle'
                            color='red'
                            disabled={busy}
                            onClick={() => {
                              if (
                                window.confirm(
                                  `Forget ${s.machine?.name ?? s.reader} and its update history? It reappears when it is next heard from.`
                                )
                              )
                                act(() => forgetScanner(api, s.reader));
                            }}
                          >
                            forget
                          </Button>
                        </Group>
                      </Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            )}

            <Text fw={600} size='sm'>
              Deploy
            </Text>
            <Group align='end' gap='xs'>
              <Select
                label='Firmware'
                placeholder={deployable.length ? 'choose' : 'none held'}
                data={deployable}
                value={firmwareId}
                onChange={setFirmwareId}
                size='xs'
                w={180}
              />
              <Select
                label='On USB'
                data={[
                  {
                    value: 'policy',
                    label: `as the policy says (${fleet.policy})`
                  },
                  { value: 'required', label: 'required: no putting off' },
                  { value: 'deferrable', label: 'deferrable' }
                ]}
                value={required}
                onChange={(v) => setRequired(v ?? 'policy')}
                size='xs'
                w={230}
              />
              <TextInput
                label='Required from'
                type='datetime-local'
                value={requiredAfter}
                onChange={(e) => setRequiredAfter(e.currentTarget.value)}
                size='xs'
              />
              <Checkbox
                label='allow a downgrade'
                checked={downgrade}
                onChange={(e) => setDowngrade(e.currentTarget.checked)}
                size='xs'
              />
            </Group>
            <Group gap='xs'>
              <Button
                size='xs'
                disabled={busy || !firmwareId || chosen.size === 0}
                onClick={() => deploy(false)}
              >
                Deploy to {chosen.size} chosen
              </Button>
              <Button
                size='xs'
                variant='default'
                disabled={busy || !firmwareId || fleet.scanners.length === 0}
                onClick={() => {
                  if (window.confirm('Deploy to every scanner?')) deploy(true);
                }}
              >
                Deploy to all
              </Button>
            </Group>
          </Stack>
        </Tabs.Panel>

        <Tabs.Panel value='releases' pt='xs'>
          <Stack gap='xs'>
            <Group gap='xs'>
              <Text size='sm'>
                From{' '}
                <Anchor
                  href={`https://github.com/${fleet.repo}/releases`}
                  target='_blank'
                  size='sm'
                >
                  {fleet.repo || '(no repository set)'}
                </Anchor>
                {fleet.last_check
                  ? `, checked ${ago(fleet.last_check.at)}`
                  : ', not checked yet'}
              </Text>
              <Button
                size='compact-xs'
                variant='default'
                disabled={busy}
                onClick={() =>
                  act(
                    () => checkReleases(api),
                    (r) =>
                      setNote(
                        r.added.length
                          ? `Fetched ${r.added.join(', ')}.`
                          : 'No new releases.'
                      )
                  )
                }
              >
                Check now
              </Button>
            </Group>
            {fleet.last_check?.errors?.length ? (
              <Text size='xs' c='red'>
                {fleet.last_check.errors.join(' · ')}
              </Text>
            ) : null}
            <Table fz='sm' verticalSpacing={4}>
              <Table.Tbody>
                {fleet.firmware.map((f) => (
                  <Table.Tr key={f.id}>
                    <Table.Td>
                      <Group gap={4}>
                        {f.release_url ? (
                          <Anchor
                            href={f.release_url}
                            target='_blank'
                            size='sm'
                          >
                            {f.version}
                          </Anchor>
                        ) : (
                          <Text size='sm'>{f.version}</Text>
                        )}
                        {f.prerelease && (
                          <Badge size='xs' variant='light'>
                            pre-release
                          </Badge>
                        )}
                        {f.version === fleet.newest && (
                          <Badge size='xs' color='green' variant='light'>
                            newest
                          </Badge>
                        )}
                      </Group>
                    </Table.Td>
                    <Table.Td>
                      <Text size='xs' c='dimmed'>
                        {f.source === 'github' ? 'GitHub' : 'uploaded'},{' '}
                        {ago(f.added_at)}, {(f.size / 1024).toFixed(0)} KB
                      </Text>
                      {f.incompatible && (
                        <Text size='xs' c='orange'>
                          {f.incompatible}
                        </Text>
                      )}
                      {!f.available && (
                        <Text size='xs' c='dimmed'>
                          image pruned
                        </Text>
                      )}
                    </Table.Td>
                    <Table.Td>
                      <Button
                        size='compact-xs'
                        variant='subtle'
                        color='red'
                        disabled={busy}
                        onClick={() => {
                          if (window.confirm(`Delete firmware ${f.version}?`))
                            act(() => deleteFirmware(api, f.id));
                        }}
                      >
                        delete
                      </Button>
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
            <Text fw={600} size='sm'>
              Upload a release
            </Text>
            <Text size='xs' c='dimmed'>
              For a server without internet access: the release's manifest.json
              and app image (and, if wanted, its merged image), as published on
              GitHub.
            </Text>
            <Group align='end' gap='xs'>
              {(['manifest', 'app', 'merged'] as const).map((k) => (
                <FileInput
                  key={k}
                  label={
                    k === 'manifest'
                      ? 'manifest.json'
                      : k === 'app'
                        ? 'App image'
                        : 'Merged image (optional)'
                  }
                  value={files[k]}
                  onChange={(f) => setFiles({ ...files, [k]: f })}
                  size='xs'
                  w={170}
                  clearable
                />
              ))}
              <Button
                size='xs'
                disabled={busy || !files.manifest || !files.app}
                onClick={() =>
                  act(
                    () =>
                      uploadRelease(
                        api,
                        Object.fromEntries(
                          Object.entries(files).filter(([, f]) => f)
                        ) as Record<string, File>
                      ),
                    (fw) => {
                      setNote(`Firmware ${fw.version} uploaded.`);
                      setFiles({ manifest: null, app: null, merged: null });
                    }
                  )
                }
              >
                Upload
              </Button>
            </Group>
          </Stack>
        </Tabs.Panel>

        <Tabs.Panel value='history' pt='xs'>
          <Table fz='sm' verticalSpacing={4}>
            <Table.Tbody>
              {history.slice(0, 50).map((d) => (
                <Table.Tr key={d.id}>
                  <Table.Td>
                    <Text size='xs'>{ago(d.requested_at)}</Text>
                  </Table.Td>
                  <Table.Td>
                    <Text size='xs'>{d.reader}</Text>
                  </Table.Td>
                  <Table.Td>
                    <DeploymentBadge d={d} />
                  </Table.Td>
                  <Table.Td>
                    <Text size='xs' c='dimmed'>
                      {d.requested_by ? `by ${d.requested_by}` : 'automatic'}
                      {d.from_version ? `, from ${d.from_version}` : ''}
                    </Text>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Tabs.Panel>
      </Tabs>
    </Stack>
  );
}

export function RenderNfcFleetItem(context: InvenTreePluginContext) {
  checkPluginVersion(context);
  return <FleetItem context={context} />;
}

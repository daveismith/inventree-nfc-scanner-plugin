/** The plugin's own API, and the one core endpoint it uses. */

import type { InvenTreePluginContext } from '@inventreedb/ui';

export const API = '/plugin/nfcscanner/api';

export interface TagPayload {
  location: number;
  text: string;
  uri: string;
  ndef: string;
  timeout_s: number;
  pwd?: string;
  pack?: string;
}

export interface Scanner {
  id: string;
  name: string;
  driver: string;
  status: string;
  status_text: string;
  online: boolean;
  last_seen: string | null;
  last_tag: Record<string, any> | null;
  warning: string | null;
}

export interface Job {
  id: number;
  kind: 'program' | 'wipe';
  location: number;
  scanner: { id: string; name: string } | null;
  created_by_name: string | null;
  created_at: string;
  finished_at: string | null;
  state:
    | 'queued'
    | 'sent'
    | 'waiting'
    | 'writing'
    | 'done'
    | 'failed'
    | 'cancelled';
  overwrite: boolean;
  uid: string;
  tag_type: string;
  protected: boolean | null;
  error: string;
  error_detail: string;
  existing_text: string;
  existing_uri: string;
}

type Api = InvenTreePluginContext['api'];

export const getTag = (api: Api, location: number) =>
  api.get(`${API}/location/${location}/tag/`).then((r) => r.data as TagPayload);

export const getScanners = (api: Api) =>
  api.get(`${API}/scanners/`).then((r) => r.data as Scanner[]);

export const getJobs = (api: Api, location: number) =>
  api
    .get(`${API}/jobs/`, { params: { location } })
    .then((r) => r.data as Job[]);

export const getJob = (api: Api, id: number) =>
  api.get(`${API}/jobs/${id}/`).then((r) => r.data as Job);

export const createJob = (
  api: Api,
  body: {
    location: number;
    scanner: string;
    kind?: string;
    overwrite?: boolean;
  }
) => api.post(`${API}/jobs/`, body).then((r) => r.data as Job);

export const cancelJob = (api: Api, id: number) =>
  api.post(`${API}/jobs/${id}/cancel/`).then((r) => r.data as Job);

export const recordUsbJob = (
  api: Api,
  location: number,
  body: Record<string, any>
) =>
  api
    .post(`${API}/location/${location}/jobs/usb/`, body)
    .then((r) => r.data as Job);

/** The tag's UID becomes the location's barcode, taken from whatever held it before. */
export const linkBarcode = (api: Api, location: number, uid: string) =>
  api
    .post(`${API}/location/${location}/link/`, { uid })
    .then((r) => r.data as { outcome: string });

// Fleet updates: the browser's side (docs/api.md, "Firmware updates").

export interface UpdateOffer {
  id: number;
  version: string;
  required: boolean;
  required_after: string | null;
  size: number;
  sha256: string;
  url: string;
  deferrals: number;
}

export interface UpdateOutcome {
  id: number;
  version: string;
  state: string;
  error: string;
  detail: string;
}

export interface CheckIn {
  reader: string;
  update: UpdateOffer | null;
  last: UpdateOutcome | null;
}

export const usbCheckIn = (
  api: Api,
  body: { reader: string; fw: string; proto: number }
) => api.post(`${API}/usb/checkin/`, body).then((r) => r.data as CheckIn);

export const usbStart = (api: Api, id: number, reader: string) =>
  api
    .post(`${API}/usb/deployments/${id}/start/`, { reader })
    .then((r) => r.data as UpdateOffer);

export const usbDefer = (api: Api, id: number, reader: string) =>
  api
    .post(`${API}/usb/deployments/${id}/defer/`, { reader })
    .then((r) => r.data as UpdateOffer);

export const usbReport = (
  api: Api,
  id: number,
  body: { reader: string; state: string; error?: string; detail?: string }
) => api.post(`${API}/usb/deployments/${id}/report/`, body);

/** A firmware image from this server, as bytes. */
export const fetchImage = (api: Api, path: string) =>
  api
    .get(path, { responseType: 'arraybuffer' })
    .then((r) => new Uint8Array(r.data as ArrayBuffer));

// Fleet updates: the admin's side.

export interface FleetDeployment {
  id: number;
  reader: string;
  version: string;
  state: string;
  via: string | null;
  from_version: string | null;
  required: boolean;
  required_flag: boolean | null;
  required_after: string | null;
  requested_by: string | null;
  requested_at: string;
  started_at: string | null;
  finished_at: string | null;
  attempts: number;
  deferrals: number;
  error: string;
  detail: string;
}

export interface FleetScanner {
  reader: string;
  fw: string | null;
  proto: number | null;
  last_seen: string | null;
  last_via: string | null;
  last_user: string | null;
  machine: { id: string; name: string } | null;
  outdated: boolean;
  deployment: FleetDeployment | null;
}

export interface FleetFirmware {
  id: number;
  version: string;
  prerelease: boolean;
  source: string;
  release_url: string;
  published_at: string | null;
  added_at: string;
  available: boolean;
  size: number;
  sha256: string;
  proto: number;
  settings_version: number;
  min_plugin: string;
  git_sha: string;
  merged: boolean;
  incompatible: string | null;
}

export interface Fleet {
  scanners: FleetScanner[];
  firmware: FleetFirmware[];
  newest: string | null;
  last_check: {
    at: string;
    added: string[];
    errors: string[];
    pruned?: string[];
  } | null;
  repo: string;
  policy: string;
}

export const getFleet = (api: Api) =>
  api.get(`${API}/fleet/`).then((r) => r.data as Fleet);

export const checkReleases = (api: Api) =>
  api.post(`${API}/fleet/check/`).then((r) => r.data);

export const uploadRelease = (api: Api, files: Record<string, File>) => {
  const form = new FormData();
  for (const [k, f] of Object.entries(files)) form.append(k, f);
  return api
    .post(`${API}/fleet/upload/`, form)
    .then((r) => r.data as FleetFirmware);
};

export const deleteFirmware = (api: Api, id: number) =>
  api.delete(`${API}/fleet/firmware/${id}/`);

export const deployFirmware = (
  api: Api,
  body: {
    firmware: number;
    scanners: string[] | 'all';
    required: boolean | null;
    required_after: string | null;
    allow_downgrade: boolean;
  }
) =>
  api.post(`${API}/fleet/deploy/`, body).then(
    (r) =>
      r.data as {
        results: { reader: string; deployment?: number; refused?: string }[];
      }
  );

export const getDeployments = (api: Api) =>
  api.get(`${API}/fleet/deployments/`).then((r) => r.data as FleetDeployment[]);

export const cancelDeployment = (api: Api, id: number) =>
  api.post(`${API}/fleet/deployments/${id}/cancel/`);

export const forgetScanner = (api: Api, reader: string) =>
  api.delete(`${API}/fleet/scanners/${reader}/`);

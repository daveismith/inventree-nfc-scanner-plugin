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

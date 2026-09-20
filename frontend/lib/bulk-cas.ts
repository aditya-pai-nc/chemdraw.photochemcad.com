import { readError } from './api'

export interface ChemicalRecord {
  status: string
  rn?: string | null
  cid?: number | null
  name?: string | null
  formula?: string | null
  weight?: number | null
  smiles?: string | null
  inchi?: string | null
  inchikey?: string | null
  url?: string | null
  note?: string | null
}

export interface BulkRow extends ChemicalRecord {
  index: number
  input: string
  cas_rn: string | null
  location: string
  source: string | null
  agreement: 'Agree' | 'Differ' | 'Not comparable'
  notes: string | null
  cas: ChemicalRecord
  pubchem: ChemicalRecord
}

export interface BulkJob {
  job_id: string
  filename: string
  status: 'queued' | 'running' | 'done' | 'cancelled' | 'error'
  total: number
  processed: number
  valid: number
  unique: number
  rows: BulkRow[]
  cancel_requested: boolean
  error: string | null
  has_exports: boolean
}

async function json<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, { cache: 'no-store', ...options })
  if (!response.ok) throw Object.assign(new Error(await readError(response)), { status: response.status })
  return response.json() as Promise<T>
}

export function createBulkJob(file: File): Promise<BulkJob> {
  const body = new FormData()
  body.append('file', file)
  return json('/api/bulk-cas', { method: 'POST', body })
}

export function getBulkJob(id: string, signal?: AbortSignal): Promise<BulkJob> {
  return json(`/api/bulk-cas/${encodeURIComponent(id)}`, { signal })
}

export function cancelBulkJob(id: string): Promise<BulkJob> {
  return json(`/api/bulk-cas/${encodeURIComponent(id)}/cancel`, { method: 'POST' })
}

export function bulkDownload(id: string, format: 'xlsx' | 'csv'): string {
  return `/api/bulk-cas/${encodeURIComponent(id)}/download/${format}`
}

export const finished = (job: BulkJob) => ['done', 'cancelled', 'error'].includes(job.status)

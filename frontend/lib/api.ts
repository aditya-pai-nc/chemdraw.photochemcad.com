import type { ChemDrawStatus, PipelineEvent } from './types'

// Always same-origin. The browser never calls FastAPI directly — Next's
// app/api/[...path] route handler proxies to it server-side, so the Python
// service can stay bound to localhost.
export function apiUrl(path: string): string {
  return path
}

export async function readError(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown }
    if (typeof body.detail === 'string') return body.detail
    if (body.detail != null) return JSON.stringify(body.detail)
  } catch {
    // ignore
  }
  return res.statusText || `Request failed (${res.status})`
}

export async function checkChemDraw(): Promise<ChemDrawStatus> {
  const res = await fetch(apiUrl('/api/chemdraw'))
  if (!res.ok) throw new Error(await readError(res))
  return res.json() as Promise<ChemDrawStatus>
}

export interface CreatedJob {
  job_id: string
  filename: string
  /** Jobs that must finish before this one starts. 0 = it has ChemDraw now. */
  queue_position: number
  /** Total jobs in the system, including the one holding ChemDraw. */
  queue_depth: number
}

export async function createJob(file: File): Promise<CreatedJob> {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch(apiUrl('/api/jobs'), { method: 'POST', body: form })
  if (!res.ok) throw new Error(await readError(res))
  return res.json() as Promise<CreatedJob>
}

export async function cancelJob(jobId: string): Promise<void> {
  const res = await fetch(apiUrl(`/api/jobs/${jobId}/cancel`), { method: 'POST' })
  if (!res.ok) throw new Error(await readError(res))
}

export function excelUrl(jobId: string): string {
  return apiUrl(`/api/jobs/${jobId}/excel`)
}

export function archiveUrl(jobId: string): string {
  return apiUrl(`/api/jobs/${jobId}/archive`)
}

export interface JobSnapshot {
  job_id: string
  filename: string
  status: 'queued' | 'running' | 'done' | 'error' | 'cancelled'
  /** Unix seconds when the job was accepted. */
  created_at: number
  done: boolean
  cancelled: boolean
  excel_path: string | null
  compound_count: number
  has_excel: boolean
  has_output: boolean
  queue_position: number
  queue_depth: number
}

export async function getJob(jobId: string): Promise<JobSnapshot> {
  const res = await fetch(apiUrl(`/api/jobs/${jobId}`))
  if (!res.ok) throw new Error(await readError(res))
  return res.json() as Promise<JobSnapshot>
}

export interface QueueSnapshot {
  depth: number
  max_depth: number
  running: { job_id: string; filename: string } | null
  waiting: { job_id: string; filename: string; position: number }[]
}

export async function getQueue(): Promise<QueueSnapshot> {
  const res = await fetch(apiUrl('/api/queue'))
  if (!res.ok) throw new Error(await readError(res))
  return res.json() as Promise<QueueSnapshot>
}

/** Live-connection state of a job's event stream. */
export type StreamStatus = 'connecting' | 'open' | 'reconnecting' | 'closed'

export function subscribeToJob(
  jobId: string,
  onEvent: (event: PipelineEvent) => void,
  onStatus?: (status: StreamStatus) => void
): () => void {
  const source = new EventSource(apiUrl(`/api/jobs/${jobId}/events`))
  onStatus?.('connecting')

  source.onopen = () => onStatus?.('open')

  source.onmessage = (message) => {
    try {
      const event = JSON.parse(message.data) as PipelineEvent
      onEvent(event)
      if (event.type === 'result' || event.type === 'error') {
        onStatus?.('closed')
        source.close()
      }
    } catch {
      // ignore malformed lines
    }
  }

  source.onerror = () => {
    // EventSource reconnects by itself and replays Last-Event-ID, so the server
    // resumes rather than repeating the job. CLOSED means it gave up for good.
    onStatus?.(source.readyState === EventSource.CLOSED ? 'closed' : 'reconnecting')
  }

  return () => source.close()
}

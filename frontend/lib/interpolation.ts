import { apiUrl, readError, type StreamStatus } from './api'

export type InterpolationMethod = 'cubic_spline' | 'akima_spline' | 'linear' | 'rbf' | 'gmm'

/** Fixed order — matches the method list in the original automation script. */
export const METHODS: InterpolationMethod[] = [
  'cubic_spline',
  'akima_spline',
  'linear',
  'rbf',
  'gmm'
]

export const METHOD_LABELS: Record<InterpolationMethod, string> = {
  cubic_spline: 'Cubic Spline',
  akima_spline: 'Akima Spline',
  linear: 'Linear',
  rbf: 'RBF (Gaussian)',
  gmm: 'Gaussian Mixture Model'
}

export const METHOD_BLURBS: Record<InterpolationMethod, string> = {
  cubic_spline: 'Smooth interpolation with continuous derivatives',
  akima_spline: 'Robust interpolation avoiding overshooting',
  linear: 'Simple linear interpolation between points',
  rbf: 'Gaussian-based radial basis function',
  gmm: 'Statistical model-based interpolation'
}

/**
 * create_verification_summary() labels methods with Python's
 * `method.replace('_', ' ').title()` — "cubic_spline" becomes "Cubic Spline",
 * "rbf" becomes "Rbf". This turns that label back into the method key.
 */
export function methodFromSummaryLabel(label: string): InterpolationMethod | null {
  const key = label.trim().toLowerCase().replace(/\s+/g, '_')
  return (METHODS as string[]).includes(key) ? (key as InterpolationMethod) : null
}

/** create_verification_summary() emits these column names verbatim. */
export interface MethodSummaryRow {
  Method: string
  MSE: number
  MAE: number
  'Max Error': number
  Correlation: number
}

export interface CompoundSummary {
  compound_id: string
  original_points: number
  interpolated_points: number
  additional_points: number
  summary: MethodSummaryRow[]
  best_method: string | null
}

export interface InterpolationMetadata {
  filename: string
  step_size: number
  id_column: string
  wavelength_column: string
  coefficient_column: string
  num_compounds: number
  file_format: '2_column' | '3_column'
}

export interface InterpolationSnapshot {
  job_id: string
  filename: string
  status: 'running' | 'done' | 'error' | 'cancelled'
  created_at: number
  done: boolean
  cancelled: boolean
  step_size: number
  has_excel: boolean
  metadata: InterpolationMetadata
  compounds: CompoundSummary[]
}

export interface CompoundSeries {
  compound_id: string
  original: { x: number[]; y: number[] }
  wavelength: number[]
  methods: Partial<Record<InterpolationMethod, number[]>>
  summary: MethodSummaryRow[]
  best_method: string | null
  original_points: number
  interpolated_points: number
}

export type InterpolationEvent =
  | { type: 'stage'; stage: number; total: number; message: string }
  | {
      type: 'compound'
      compound_id: string
      index: number
      total: number
      original_points: number
      interpolated_points: number
      best_method: string | null
    }
  | { type: 'log'; level: 'info' | 'warn' | 'error'; message: string }
  | { type: 'result'; success: boolean; compoundCount: number; pointCount: number }
  | { type: 'error'; message: string }

export async function createInterpolation(
  file: File,
  stepSize: number
): Promise<{ job_id: string; filename: string; step_size: number }> {
  const form = new FormData()
  form.append('file', file)
  form.append('step_size', String(stepSize))
  const res = await fetch(apiUrl('/api/interpolation'), { method: 'POST', body: form })
  if (!res.ok) throw new Error(await readError(res))
  return res.json()
}

export async function getInterpolation(jobId: string): Promise<InterpolationSnapshot> {
  const res = await fetch(apiUrl(`/api/interpolation/${jobId}`))
  if (!res.ok) throw new Error(await readError(res))
  return res.json() as Promise<InterpolationSnapshot>
}

export async function getCompoundSeries(
  jobId: string,
  compoundId: string
): Promise<CompoundSeries> {
  const res = await fetch(
    apiUrl(`/api/interpolation/${jobId}/compounds/${encodeURIComponent(compoundId)}`)
  )
  if (!res.ok) throw new Error(await readError(res))
  return res.json() as Promise<CompoundSeries>
}

export async function cancelInterpolation(jobId: string): Promise<void> {
  const res = await fetch(apiUrl(`/api/interpolation/${jobId}/cancel`), { method: 'POST' })
  if (!res.ok) throw new Error(await readError(res))
}

export function interpolationExcelUrl(jobId: string): string {
  return apiUrl(`/api/interpolation/${jobId}/excel`)
}

export function subscribeToInterpolation(
  jobId: string,
  onEvent: (event: InterpolationEvent) => void,
  onStatus?: (status: StreamStatus) => void
): () => void {
  const source = new EventSource(apiUrl(`/api/interpolation/${jobId}/events`))
  onStatus?.('connecting')

  source.onopen = () => onStatus?.('open')

  source.onmessage = (message) => {
    try {
      const event = JSON.parse(message.data) as InterpolationEvent
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
    onStatus?.(source.readyState === EventSource.CLOSED ? 'closed' : 'reconnecting')
  }

  return () => source.close()
}

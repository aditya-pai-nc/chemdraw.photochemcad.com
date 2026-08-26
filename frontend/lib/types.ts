export type PipelineEvent =
  | { type: 'stage'; stage: number; total: number; message: string }
  | { type: 'compound'; name: string; match: string; index: number; total: number }
  | { type: 'log'; level: 'info' | 'warn' | 'error'; message: string }
  | { type: 'queue'; position: number; depth: number; message: string }
  | { type: 'result'; success: boolean; excelPath: string; compoundCount: number; outputDir: string }
  | { type: 'error'; message: string }
  | { type: 'chemdraw_status'; available: boolean; version?: string }

export interface CompoundRow {
  name: string
  match: string
  index: number
}

export type AppScreen = 'home' | 'processing' | 'results'

export interface ChemDrawStatus {
  available: boolean
  version?: string
  progid?: string
  reason?: string
}

/**
 * A match verdict. Only three states — the pipeline makes exact comparisons and
 * reports them plainly:
 *   '✅' agree, '❌' disagree, '—' one side is missing so there is nothing to compare.
 */
export type MatchSymbol = '✅' | '❌' | '—'

export type CasVerification = 'Verified' | 'Mismatch' | 'Not comparable' | 'Not found' |
  'Unavailable' | 'Not configured' | 'Disabled' | 'Skipped'

export interface CasResult {
  casVerification?: CasVerification
  casDetail?: string | null
  casRn?: string | null
  casLink?: string | null
}

export type PipelineEvent =
  | { type: 'stage'; stage: number; total: number; message: string }
  | (CasResult & {
      type: 'compound'
      name: string
      /** PubChem's formula equals the drawn formula, and its weight to within 0.5. */
      match: MatchSymbol
      /** Canonical SMILES from ChemDraw vs canonical SMILES from PubChem. */
      inchikeyMatch: MatchSymbol
      index: number
      total: number
    })
  | {
      /** One unmatched compound came back from the curation model. */
      type: 'curated'
      name: string
      index: number
      verdict?: 'yes' | 'no' | 'uncertain'
      progress: number
      total: number
    }
  | { type: 'log'; level: 'info' | 'warn' | 'error'; message: string }
  | { type: 'queue'; position: number; depth: number; message: string }
  | {
      type: 'result'
      success: boolean
      excelPath: string
      compoundCount: number
      outputDir: string
      matchCount?: number
      inchikeyMatchCount?: number
      casVerifiedCount?: number
      curatedCount?: number
    }
  | { type: 'error'; message: string }
  | { type: 'chemdraw_status'; available: boolean; version?: string }

export interface CompoundRow extends CasResult {
  name: string
  match: MatchSymbol
  inchikeyMatch: MatchSymbol
  index: number
  /** Set once the curation model has reported on this compound. */
  curated?: 'yes' | 'no' | 'uncertain' | null
}

export type AppScreen = 'home' | 'processing' | 'results'

export interface ChemDrawStatus {
  available: boolean
  version?: string
  progid?: string
  reason?: string
}

/** Whether the curation pass for unmatched compounds is configured to run. */
export interface AiStatus {
  enabled: boolean
  ready: boolean
  curate_model: string
  concurrency: number
  has_credentials: boolean
  reason?: string | null
}

/**
 * A match verdict. The pipeline reports three of these per compound, from
 * sources that can and do disagree:
 *   '✅' agreement, '🟡' same skeleton but different stereochemistry or
 *   protonation, '❌' disagreement, '—' nothing to compare.
 */
export type MatchSymbol = '✅' | '🟡' | '❌' | '—'

export type PipelineEvent =
  | { type: 'stage'; stage: number; total: number; message: string }
  | {
      type: 'compound'
      name: string
      /** Formula and weight against PubChem — the original verdict. */
      match: MatchSymbol
      /** Structure hash against PubChem. Stricter, and often succeeds where `match` fails. */
      inchikeyMatch: MatchSymbol
      /** Claude's independent identification against the drawn structure. */
      aiMatch: MatchSymbol
      index: number
      total: number
      /**
       * Which pass produced this. Every compound is reported twice: once after
       * ChemDraw/RDKit/PubChem, then again once the AI pass has run, so rows
       * must be updated in place by `index` rather than appended.
       */
      stage?: 'structure' | 'ai'
      aiProgress?: number
      aiTotal?: number
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
      inchikeyPartialCount?: number
      aiMatchCount?: number
      needsReviewCount?: number
    }
  | { type: 'error'; message: string }
  | { type: 'chemdraw_status'; available: boolean; version?: string }

export interface CompoundRow {
  name: string
  match: MatchSymbol
  inchikeyMatch: MatchSymbol
  aiMatch: MatchSymbol
  index: number
  /** False until the AI pass has reported on this compound. */
  aiDone: boolean
}

export type AppScreen = 'home' | 'processing' | 'results'

export interface ChemDrawStatus {
  available: boolean
  version?: string
  progid?: string
  reason?: string
}

/** Whether the AI identification and consensus pass is configured to run. */
export interface AiStatus {
  enabled: boolean
  ready: boolean
  identify_model: string
  consensus_model: string
  web_search: boolean
  has_credentials: boolean
  reason?: string | null
}

'use client'

import { useEffect, useRef, type JSX } from 'react'
import { XCircle, Loader2, Clock, WifiOff } from 'lucide-react'
import type { CompoundRow } from '@/lib/types'
import type { StreamStatus } from '@/lib/api'

const STAGE_LABELS = [
  { label: 'CDX → CDXML', desc: 'Opening ChemDraw and converting file' },
  { label: 'Split Molecules', desc: 'Extracting individual structures' },
  { label: 'Process & Enrich', desc: 'RDKit + PubChem enrichment' }
]

interface Props {
  filename: string
  currentStage: number
  stageMessage: string
  compounds: CompoundRow[]
  totalCompounds: number
  logs: { level: string; text: string }[]
  /** Jobs ahead of this one in the ChemDraw queue. 0 = running now. */
  queuePosition: number
  /** Total jobs in the ChemDraw queue, including the running one. */
  queueDepth: number
  /** Live-connection state of the event stream. */
  streamStatus: StreamStatus
  /** Time since the server accepted the job. */
  elapsedMs: number
  onCancel: () => void
}

export function ProcessingScreen({
  filename, currentStage, stageMessage, compounds, totalCompounds, logs,
  queuePosition, queueDepth, streamStatus, elapsedMs, onCancel
}: Props): JSX.Element {
  const logEndRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [logs])

  const queued = queuePosition > 0
  const reconnecting = streamStatus === 'reconnecting' || streamStatus === 'connecting'

  const totalSeconds = Math.max(0, Math.floor(elapsedMs / 1000))
  const elapsed = totalSeconds < 60
    ? `${totalSeconds}s`
    : `${Math.floor(totalSeconds / 60)}m ${String(totalSeconds % 60).padStart(2, '0')}s`
  const matched = compounds.filter((c) => c.match === '✅').length
  const failed = compounds.filter((c) => c.match === '❌').length

  return (
    <div className="flex flex-col h-full px-6 py-5 gap-4">
      <div className="flex items-center justify-between shrink-0">
        <div>
          <h2 className="text-base font-semibold text-white">{queued ? 'Queued' : 'Processing'}</h2>
          <p className="text-xs text-slate-500 font-mono mt-0.5">
            {filename}
            <span className="ml-2 text-slate-600">· {elapsed} elapsed</span>
          </p>
        </div>
        <button
          onClick={onCancel}
          className="flex items-center gap-1.5 text-xs text-slate-500 hover:text-red-400 transition-colors px-3 py-1.5 rounded-lg hover:bg-red-900/20"
        >
          <XCircle className="w-3.5 h-3.5" />
          Cancel
        </button>
      </div>

      <div className="flex gap-2 shrink-0">
        {STAGE_LABELS.map((s, i) => {
          const stageNum = i + 1
          const state =
            currentStage > stageNum ? 'done' :
            currentStage === stageNum ? 'active' : 'pending'
          return (
            <div
              key={i}
              className={`flex-1 rounded-xl px-3 py-2.5 border transition-all duration-300 ${
                state === 'done' ? 'border-emerald-700/50 bg-emerald-900/20' :
                state === 'active' ? 'border-brand-500/60 bg-brand-900/20' :
                'border-slate-800 bg-slate-900/40'
              }`}
            >
              <div className="flex items-center gap-2 mb-1">
                <div className={`w-5 h-5 rounded-full flex items-center justify-center text-xs font-bold shrink-0 ${
                  state === 'done' ? 'bg-emerald-500 text-white' :
                  state === 'active' ? 'bg-brand-500 text-white' :
                  'bg-slate-700 text-slate-500'
                }`}>
                  {state === 'done' ? '✓' : stageNum}
                </div>
                {state === 'active' && <Loader2 className="w-3 h-3 text-brand-400 animate-spin" />}
                <span className={`text-xs font-semibold ${
                  state === 'done' ? 'text-emerald-400' :
                  state === 'active' ? 'text-brand-300' :
                  'text-slate-600'
                }`}>{s.label}</span>
              </div>
              <p className={`text-[10px] leading-snug ${
                state === 'active' ? 'text-slate-400' : 'text-slate-600'
              }`}>{s.desc}</p>
            </div>
          )
        })}
      </div>

      {reconnecting && (
        <div className="shrink-0 px-3 py-2 rounded-lg bg-slate-900 border border-slate-700 text-xs text-slate-400 flex items-center gap-2">
          <WifiOff className="w-3.5 h-3.5 text-amber-400 shrink-0" />
          Reconnecting to the live log — your job keeps running on the server.
        </div>
      )}

      {queued ? (
        <div className="shrink-0 px-4 py-3 rounded-xl bg-amber-900/20 border border-amber-700/40 flex items-center gap-3">
          <Clock className="w-4 h-4 text-amber-400 shrink-0" />
          <div className="min-w-0">
            <p className="text-xs font-semibold text-amber-300">
              Waiting for ChemDraw — {queuePosition} {queuePosition === 1 ? 'job' : 'jobs'} ahead of you
            </p>
            <p className="text-[11px] text-amber-500/80 mt-0.5">
              ChemDraw processes one file at a time. Yours starts automatically when it reaches
              the front{queueDepth > 1 ? ` of the ${queueDepth}-job queue` : ''}.
            </p>
          </div>
        </div>
      ) : stageMessage ? (
        <div className="shrink-0 px-3 py-2 rounded-lg bg-slate-900 border border-slate-800 text-xs text-slate-400 flex items-center gap-2">
          <Loader2 className="w-3 h-3 text-brand-400 animate-spin shrink-0" />
          {stageMessage}
        </div>
      ) : null}

      {totalCompounds > 0 && (
        <div className="shrink-0 space-y-1.5">
          <div className="flex items-center justify-between text-xs text-slate-500">
            <span>Compounds: {compounds.length} / {totalCompounds}</span>
            <span className="flex items-center gap-2">
              <span className="text-emerald-400">{matched} matched</span>
              {failed > 0 && <span className="text-red-400">{failed} unmatched</span>}
            </span>
          </div>
          <div className="h-1.5 rounded-full bg-slate-800 overflow-hidden">
            <div
              className="h-full bg-brand-500 rounded-full transition-all duration-300"
              style={{ width: `${Math.min(100, (compounds.length / totalCompounds) * 100)}%` }}
            />
          </div>
        </div>
      )}

      <div className="flex-1 min-h-0 rounded-xl bg-[#080b10] border border-slate-800 overflow-hidden flex flex-col">
        <div className="px-3 py-2 border-b border-slate-800 shrink-0">
          <span className="text-[10px] font-semibold text-slate-600 uppercase tracking-widest">Live Log</span>
        </div>
        <div className="flex-1 overflow-y-auto px-3 py-2 font-mono text-[11px] leading-relaxed space-y-0.5">
          {logs.map((log, i) => (
            <p
              key={i}
              className={
                log.level === 'error' ? 'text-red-400' :
                log.level === 'warn' ? 'text-amber-400' :
                'text-slate-400'
              }
            >
              {log.text}
            </p>
          ))}
          <div ref={logEndRef} />
        </div>
      </div>
    </div>
  )
}

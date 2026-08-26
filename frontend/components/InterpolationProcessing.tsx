'use client'

import { useEffect, useRef, type JSX } from 'react'
import { XCircle, Loader2, WifiOff } from 'lucide-react'
import type { StreamStatus } from '@/lib/api'

const STAGE_LABELS = [
  { label: 'Parse', desc: 'Reading the uploaded data file' },
  { label: 'Interpolate', desc: '5 methods + verification per compound' },
  { label: 'Export', desc: 'Writing the Excel workbook' }
]

export interface CompoundProgress {
  compoundId: string
  index: number
  originalPoints: number
  interpolatedPoints: number
  bestMethod: string | null
}

interface Props {
  filename: string
  currentStage: number
  stageMessage: string
  compounds: CompoundProgress[]
  totalCompounds: number
  logs: { level: string; text: string }[]
  streamStatus: StreamStatus
  onCancel: () => void
}

export function InterpolationProcessing({
  filename, currentStage, stageMessage, compounds, totalCompounds, logs, streamStatus, onCancel
}: Props): JSX.Element {
  const logEndRef = useRef<HTMLDivElement>(null)
  const reconnecting = streamStatus === 'reconnecting' || streamStatus === 'connecting'

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [logs])

  return (
    <div className="flex flex-col h-full px-6 py-5 gap-4">
      <div className="flex items-center justify-between shrink-0">
        <div>
          <h2 className="text-base font-semibold text-white">Interpolating</h2>
          <p className="text-xs text-slate-500 font-mono mt-0.5">{filename}</p>
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
        {STAGE_LABELS.map((stage, i) => {
          const stageNum = i + 1
          const state =
            currentStage > stageNum ? 'done' : currentStage === stageNum ? 'active' : 'pending'
          return (
            <div
              key={stage.label}
              className={`flex-1 rounded-xl px-3 py-2.5 border transition-all duration-300 ${
                state === 'done'
                  ? 'border-emerald-700/50 bg-emerald-900/20'
                  : state === 'active'
                    ? 'border-brand-500/60 bg-brand-900/20'
                    : 'border-slate-800 bg-slate-900/40'
              }`}
            >
              <div className="flex items-center gap-2 mb-1">
                <div
                  className={`w-5 h-5 rounded-full flex items-center justify-center text-xs font-bold shrink-0 ${
                    state === 'done'
                      ? 'bg-emerald-500 text-white'
                      : state === 'active'
                        ? 'bg-brand-500 text-white'
                        : 'bg-slate-700 text-slate-500'
                  }`}
                >
                  {state === 'done' ? '✓' : stageNum}
                </div>
                {state === 'active' && <Loader2 className="w-3 h-3 text-brand-400 animate-spin" />}
                <span
                  className={`text-xs font-semibold ${
                    state === 'done'
                      ? 'text-emerald-400'
                      : state === 'active'
                        ? 'text-brand-300'
                        : 'text-slate-600'
                  }`}
                >
                  {stage.label}
                </span>
              </div>
              <p className={`text-[10px] leading-snug ${state === 'active' ? 'text-slate-400' : 'text-slate-600'}`}>
                {stage.desc}
              </p>
            </div>
          )
        })}
      </div>

      {reconnecting && (
        <div className="shrink-0 px-3 py-2 rounded-lg bg-slate-900 border border-slate-700 text-xs text-slate-400 flex items-center gap-2">
          <WifiOff className="w-3.5 h-3.5 text-amber-400 shrink-0" />
          Reconnecting to the live log — your run keeps going on the server.
        </div>
      )}

      {stageMessage && (
        <div className="shrink-0 px-3 py-2 rounded-lg bg-slate-900 border border-slate-800 text-xs text-slate-400 flex items-center gap-2">
          <Loader2 className="w-3 h-3 text-brand-400 animate-spin shrink-0" />
          {stageMessage}
        </div>
      )}

      {totalCompounds > 0 && (
        <div className="shrink-0 space-y-1.5">
          <div className="flex items-center justify-between text-xs text-slate-500">
            <span>Compounds: {compounds.length} / {totalCompounds}</span>
            {compounds.length > 0 && (
              <span className="text-slate-600">
                latest: {compounds[compounds.length - 1].compoundId}
              </span>
            )}
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
          <span className="text-[10px] font-semibold text-slate-600 uppercase tracking-widest">
            Live Log
          </span>
        </div>
        <div className="flex-1 overflow-y-auto px-3 py-2 font-mono text-[11px] leading-relaxed space-y-0.5">
          {logs.map((log, i) => (
            <p
              key={i}
              className={
                log.level === 'error'
                  ? 'text-red-400'
                  : log.level === 'warn'
                    ? 'text-amber-400'
                    : 'text-slate-400'
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

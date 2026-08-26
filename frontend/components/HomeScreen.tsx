'use client'

import { useRef, useState, type JSX } from 'react'
import { FileUp, Play, FlaskConical, AlertTriangle, Clock, Loader2 } from 'lucide-react'

interface Props {
  file: File | null
  chemDrawAvailable: boolean | null
  chemDrawReason?: string | null
  /** Jobs currently in the ChemDraw queue, so the wait is no surprise. */
  queueDepth: number
  /** True while a job left over from a previous page load is being picked up. */
  resuming: boolean
  onFileSelected: (file: File) => void
  onStart: () => void
}

export function HomeScreen({
  file,
  chemDrawAvailable,
  chemDrawReason,
  queueDepth,
  resuming,
  onFileSelected,
  onStart
}: Props): JSX.Element {
  const [dragging, setDragging] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  const canStart = !!file && chemDrawAvailable !== false && !resuming

  const acceptFile = (next: File | undefined): void => {
    if (next && next.name.toLowerCase().endsWith('.cdx')) {
      onFileSelected(next)
    }
  }

  return (
    <div className="flex flex-col items-center justify-center h-full px-8 gap-8">
      <div className="text-center space-y-2">
        <div className="flex items-center justify-center mb-4">
          <div className="p-4 rounded-2xl bg-brand-600/20 border border-brand-500/30">
            <FlaskConical className="w-10 h-10 text-brand-400" />
          </div>
        </div>
        <h1 className="text-3xl font-bold text-white">ChemDraw Processor</h1>
        <p className="text-slate-400 text-sm max-w-md">
          Upload a ChemDraw file (.cdx) to extract molecular structures, generate SMILES, and enrich with PubChem data — all in one click.
        </p>
      </div>

      {chemDrawAvailable === false && (
        <div className="px-4 py-3 rounded-xl bg-red-900/30 border border-red-700/50 text-red-300 text-sm max-w-lg w-full space-y-1.5">
          <div className="flex items-center gap-3">
            <AlertTriangle className="w-4 h-4 shrink-0" />
            <span>ChemDraw is not installed or not detectable. Stage 1 (CDX → CDXML) and Stage 3 (MOL export) require ChemDraw on the backend host.</span>
          </div>
          {chemDrawReason && (
            <p className="text-xs text-red-200/90 font-mono break-words">
              Detector info: {chemDrawReason}
            </p>
          )}
        </div>
      )}

      {resuming && (
        <div className="px-4 py-3 rounded-xl bg-slate-900 border border-slate-800 text-slate-400 text-sm max-w-lg w-full flex items-center gap-3">
          <Loader2 className="w-4 h-4 shrink-0 animate-spin text-brand-400" />
          <span>Reconnecting to a job from your last visit…</span>
        </div>
      )}

      {queueDepth > 0 && (
        <div className="px-4 py-3 rounded-xl bg-amber-900/20 border border-amber-700/40 text-amber-300 text-sm max-w-lg w-full flex items-center gap-3">
          <Clock className="w-4 h-4 shrink-0 text-amber-400" />
          <span>
            ChemDraw is busy — {queueDepth} {queueDepth === 1 ? 'job is' : 'jobs are'} in the
            queue. Yours will start automatically when it reaches the front.
          </span>
        </div>
      )}

      <div className="w-full max-w-lg space-y-3">
        <input
          ref={inputRef}
          type="file"
          accept=".cdx"
          className="hidden"
          onChange={(e) => {
            acceptFile(e.target.files?.[0])
            e.currentTarget.value = ''
          }}
        />

        <div
          onClick={() => inputRef.current?.click()}
          onDragOver={(e) => {
            e.preventDefault()
            setDragging(true)
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault()
            setDragging(false)
            acceptFile(e.dataTransfer.files[0])
          }}
          className={`
            relative cursor-pointer rounded-2xl border-2 border-dashed transition-all duration-200 p-8
            flex flex-col items-center justify-center gap-3 group
            ${dragging
              ? 'border-brand-400 bg-brand-500/10'
              : file
                ? 'border-emerald-500/60 bg-emerald-900/10'
                : 'border-slate-700 bg-slate-900/50 hover:border-brand-500/60 hover:bg-brand-900/10'
            }
          `}
        >
          <FileUp className={`w-8 h-8 transition-colors ${file ? 'text-emerald-400' : 'text-slate-500 group-hover:text-brand-400'}`} />
          {file ? (
            <>
              <p className="text-sm font-semibold text-emerald-300">{file.name}</p>
              <p className="text-xs text-slate-500">{(file.size / 1024).toFixed(1)} KB</p>
              <p className="text-xs text-slate-600">Click to change file</p>
            </>
          ) : (
            <>
              <p className="text-sm font-medium text-slate-300">Drop your .cdx file here</p>
              <p className="text-xs text-slate-500">or click to browse</p>
            </>
          )}
        </div>

        <p className="text-center text-xs text-slate-500">
          Results are generated on the server. You can download the Excel workbook and a ZIP of MOL files, images, and split CDXML when processing finishes.
        </p>

        <button
          onClick={onStart}
          disabled={!canStart}
          className={`
            w-full flex items-center justify-center gap-2 px-6 py-3.5 rounded-xl font-semibold text-sm transition-all duration-200
            ${canStart
              ? 'bg-brand-600 hover:bg-brand-500 text-white shadow-lg shadow-brand-900/40 active:scale-[0.98]'
              : 'bg-slate-800 text-slate-600 cursor-not-allowed'
            }
          `}
        >
          <Play className="w-4 h-4" />
          Start Processing
        </button>

        {!file && (
          <p className="text-center text-xs text-slate-600">Select a .cdx file to begin</p>
        )}
      </div>
    </div>
  )
}

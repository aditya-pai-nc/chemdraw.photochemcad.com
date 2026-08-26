'use client'

import { useRef, useState, type JSX } from 'react'
import { ChartSpline, Play, Table, Loader2, Info } from 'lucide-react'
import { METHODS, METHOD_BLURBS, METHOD_LABELS } from '@/lib/interpolation'

const ACCEPT = '.txt,.tsv,.csv,.xlsx,.xls'

interface Props {
  file: File | null
  stepSize: number
  resuming: boolean
  onFileSelected: (file: File) => void
  onStepSize: (step: number) => void
  onStart: () => void
}

export function InterpolationHome({
  file, stepSize, resuming, onFileSelected, onStepSize, onStart
}: Props): JSX.Element {
  const [dragging, setDragging] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  const canStart = !!file && !resuming && stepSize >= 0.01 && stepSize <= 100

  const acceptFile = (next: File | undefined): void => {
    if (!next) return
    const ext = next.name.split('.').pop()?.toLowerCase()
    if (ext && ['txt', 'tsv', 'csv', 'xlsx', 'xls'].includes(ext)) onFileSelected(next)
  }

  return (
    <div className="h-full overflow-y-auto px-8 py-8">
      <div className="max-w-5xl mx-auto grid lg:grid-cols-2 gap-8">
        <div className="space-y-4">
          <div>
            <div className="p-3 rounded-2xl bg-brand-600/20 border border-brand-500/30 w-fit mb-3">
              <ChartSpline className="w-7 h-7 text-brand-400" />
            </div>
            <h1 className="text-2xl font-bold text-white">Spectral Interpolation</h1>
            <p className="text-slate-400 text-sm mt-1">
              Upload spectral data to interpolate it with five methods, then verify each one by
              predicting the measured points from the generated ones.
            </p>
          </div>

          {resuming && (
            <div className="px-4 py-3 rounded-xl bg-slate-900 border border-slate-800 text-slate-400 text-sm flex items-center gap-3">
              <Loader2 className="w-4 h-4 shrink-0 animate-spin text-brand-400" />
              <span>Reconnecting to a run from your last visit…</span>
            </div>
          )}

          <input
            ref={inputRef}
            type="file"
            accept={ACCEPT}
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
            <Table className={`w-8 h-8 transition-colors ${file ? 'text-emerald-400' : 'text-slate-500 group-hover:text-brand-400'}`} />
            {file ? (
              <>
                <p className="text-sm font-semibold text-emerald-300">{file.name}</p>
                <p className="text-xs text-slate-500">{(file.size / 1024).toFixed(1)} KB</p>
                <p className="text-xs text-slate-600">Click to change file</p>
              </>
            ) : (
              <>
                <p className="text-sm font-medium text-slate-300">Drop your data file here</p>
                <p className="text-xs text-slate-500">or click to browse — .txt, .tsv, .csv, .xlsx, .xls (max 16 MB)</p>
              </>
            )}
          </div>

          <div>
            <label htmlFor="stepSize" className="block text-xs font-medium text-slate-400 mb-1.5">
              Interpolation step size (nm)
            </label>
            <input
              id="stepSize"
              type="number"
              min={0.1}
              max={10}
              step={0.1}
              value={stepSize}
              onChange={(e) => onStepSize(Number(e.target.value))}
              className="w-full px-3 py-2 rounded-lg bg-slate-900 border border-slate-700 text-sm text-slate-200 focus:outline-none focus:border-brand-500"
            />
            <p className="text-xs text-slate-600 mt-1">
              Smaller values create more detailed interpolation (e.g. 0.5 for every 0.5 nm).
            </p>
          </div>

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
            Start Interpolation
          </button>
        </div>

        <div className="space-y-4">
          <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-4">
            <h3 className="text-xs font-semibold text-slate-300 uppercase tracking-widest mb-3 flex items-center gap-2">
              <Info className="w-3.5 h-3.5 text-slate-500" />
              File format
            </h3>
            <div className="space-y-3 text-xs">
              <div>
                <p className="font-semibold text-slate-300">2 columns (most common)</p>
                <p className="text-slate-500 mt-0.5">
                  Wavelength, then coefficient/intensity. The compound id comes from the filename.
                </p>
                <pre className="mt-1.5 p-2 rounded bg-slate-950 border border-slate-800 text-[10px] text-slate-400 overflow-x-auto">{`wavelength,coefficient
400,0.07956
420,0.21627
440,0.45783`}</pre>
              </div>
              <div>
                <p className="font-semibold text-slate-300">3 columns</p>
                <p className="text-slate-500 mt-0.5">
                  Id, wavelength, coefficient — for files holding several compounds.
                </p>
                <pre className="mt-1.5 p-2 rounded bg-slate-950 border border-slate-800 text-[10px] text-slate-400 overflow-x-auto">{`compound_id,wavelength,coefficient
CMPD001,400,0.07956
CMPD001,420,0.21627
CMPD002,400,0.11240`}</pre>
              </div>
            </div>
          </div>

          <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-4">
            <h3 className="text-xs font-semibold text-slate-300 uppercase tracking-widest mb-3">
              Methods applied
            </h3>
            <ol className="space-y-2">
              {METHODS.map((method, i) => (
                <li key={method} className="flex gap-3">
                  <span className="w-5 h-5 rounded-full bg-slate-800 text-slate-500 text-[10px] font-bold flex items-center justify-center shrink-0">
                    {i + 1}
                  </span>
                  <div>
                    <p className="text-xs font-semibold text-slate-300">{METHOD_LABELS[method]}</p>
                    <p className="text-[11px] text-slate-500">{METHOD_BLURBS[method]}</p>
                  </div>
                </li>
              ))}
            </ol>
          </div>

          <div className="rounded-xl border border-emerald-800/40 bg-emerald-900/10 p-4">
            <h3 className="text-xs font-semibold text-emerald-300 mb-2">What you get</h3>
            <ul className="text-[11px] text-emerald-600/90 space-y-1 list-disc list-inside">
              <li>A chart per method against the measured points</li>
              <li>Methods ranked by how well they rebuild the measured data</li>
              <li>An Excel workbook with every interpolated point</li>
            </ul>
          </div>
        </div>
      </div>
    </div>
  )
}

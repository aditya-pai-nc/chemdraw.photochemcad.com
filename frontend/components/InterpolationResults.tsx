'use client'

import { useMemo, type JSX } from 'react'
import { Download, RotateCcw, AlertCircle, Loader2, TrendingUp } from 'lucide-react'
import { SpectrumChart, formatValue } from '@/components/SpectrumChart'
import {
  METHODS,
  METHOD_LABELS,
  interpolationExcelUrl,
  methodFromSummaryLabel,
  type CompoundSeries,
  type InterpolationSnapshot
} from '@/lib/interpolation'

// One validated hue carries every interpolated curve. Each panel is titled with
// its method, so colour does no identity work — five overlaid hues would fail
// the all-pairs CVD gate and tell the reader nothing extra.
const CURVE = '#3987e5'

interface Props {
  snapshot: InterpolationSnapshot
  series: CompoundSeries | null
  seriesLoading: boolean
  selected: string | null
  errorMessage: string | null
  onSelect: (compoundId: string) => void
  onReset: () => void
}

export function InterpolationResults({
  snapshot, series, seriesLoading, selected, errorMessage, onSelect, onReset
}: Props): JSX.Element {
  const summary = snapshot.compounds.find((c) => c.compound_id === selected) ?? null

  // A shared y-scale across the panels — otherwise the small multiples would
  // not be comparable, which is the entire point of showing them together.
  const domains = useMemo(() => {
    if (!series) return null
    const ys = [...series.original.y]
    for (const method of METHODS) {
      const values = series.methods[method]
      if (values) ys.push(...values)
    }
    const xs = series.wavelength.length ? series.wavelength : series.original.x
    const yMin = Math.min(...ys)
    const yMax = Math.max(...ys)
    const padding = (yMax - yMin) * 0.08 || 0.1
    return {
      x: [Math.min(...xs), Math.max(...xs)] as [number, number],
      y: [yMin - padding, yMax + padding] as [number, number]
    }
  }, [series])

  const bestMethod = summary?.best_method ? methodFromSummaryLabel(summary.best_method) : null

  return (
    <div className="h-full overflow-y-auto px-6 py-5">
      <div className="max-w-6xl mx-auto space-y-5">
        {errorMessage ? (
          <div className="flex items-center gap-3 px-4 py-3 rounded-xl bg-red-900/30 border border-red-700/50">
            <AlertCircle className="w-5 h-5 text-red-400 shrink-0" />
            <div>
              <p className="text-sm font-semibold text-red-300">Interpolation failed</p>
              <p className="text-xs text-red-500 mt-0.5 whitespace-pre-wrap">{errorMessage}</p>
            </div>
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-3">
            <div className="min-w-0">
              <h2 className="text-base font-semibold text-white">Interpolation results</h2>
              <p className="text-xs text-slate-500 font-mono mt-0.5 truncate">
                {snapshot.metadata.filename} · {snapshot.step_size} nm step ·{' '}
                {snapshot.metadata.num_compounds}{' '}
                {snapshot.metadata.num_compounds === 1 ? 'compound' : 'compounds'} ·{' '}
                {snapshot.metadata.file_format === '2_column' ? '2-column' : '3-column'} file
              </p>
            </div>
            <div className="flex gap-2 ml-auto">
              {snapshot.has_excel && (
                <a
                  href={interpolationExcelUrl(snapshot.job_id)}
                  className="flex items-center gap-2 px-4 py-2.5 rounded-xl bg-brand-600 hover:bg-brand-500 text-white text-sm font-semibold transition-colors"
                >
                  <Download className="w-4 h-4" />
                  Download Excel
                </a>
              )}
              <button
                onClick={onReset}
                className="flex items-center gap-2 px-4 py-2.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-400 text-sm transition-colors"
              >
                <RotateCcw className="w-4 h-4" />
                New file
              </button>
            </div>
          </div>
        )}

        {snapshot.compounds.length > 1 && (
          <div className="space-y-1.5">
            <p className="text-[10px] font-semibold text-slate-600 uppercase tracking-widest">
              Compound
            </p>
            <div className="flex flex-wrap gap-1.5">
              {snapshot.compounds.map((compound) => (
                <button
                  key={compound.compound_id}
                  onClick={() => onSelect(compound.compound_id)}
                  className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors border ${
                    compound.compound_id === selected
                      ? 'bg-brand-600/20 text-brand-300 border-brand-500/40'
                      : 'bg-slate-900 text-slate-400 border-slate-800 hover:border-slate-700 hover:text-slate-300'
                  }`}
                >
                  {compound.compound_id}
                </button>
              ))}
            </div>
          </div>
        )}

        {summary && (
          <div className="grid grid-cols-3 gap-3">
            <div className="rounded-xl bg-slate-900 border border-slate-800 px-4 py-3 text-center">
              <p className="text-2xl font-bold text-white">{summary.original_points}</p>
              <p className="text-xs text-slate-500 mt-0.5">Measured points</p>
            </div>
            <div className="rounded-xl bg-brand-900/20 border border-brand-800/40 px-4 py-3 text-center">
              <p className="text-2xl font-bold text-brand-300">+{summary.additional_points}</p>
              <p className="text-xs text-slate-500 mt-0.5">Generated points</p>
            </div>
            <div className="rounded-xl bg-slate-900 border border-slate-800 px-4 py-3 text-center">
              <p className="text-2xl font-bold text-white">{summary.interpolated_points}</p>
              <p className="text-xs text-slate-500 mt-0.5">Total per method</p>
            </div>
          </div>
        )}

        {summary && summary.summary.length > 0 && (
          <div className="rounded-xl border border-slate-800 overflow-hidden">
            <div className="px-4 py-2.5 bg-slate-900 border-b border-slate-800 flex items-center gap-2">
              <TrendingUp className="w-3.5 h-3.5 text-slate-500" />
              <span className="text-xs font-semibold text-slate-500 uppercase tracking-widest">
                Method accuracy — ranked by MSE
              </span>
              <span className="text-[10px] text-slate-600 ml-auto">
                measured points rebuilt from the generated ones
              </span>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="bg-slate-900/60">
                  <tr>
                    <th className="text-left px-4 py-2 text-slate-500 font-medium">Method</th>
                    <th className="text-right px-4 py-2 text-slate-500 font-medium">MSE</th>
                    <th className="text-right px-4 py-2 text-slate-500 font-medium">MAE</th>
                    <th className="text-right px-4 py-2 text-slate-500 font-medium">Max error</th>
                    <th className="text-right px-4 py-2 text-slate-500 font-medium">Correlation</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/50">
                  {summary.summary.map((row, i) => {
                    const key = methodFromSummaryLabel(row.Method)
                    return (
                      <tr key={row.Method} className="hover:bg-slate-800/30 transition-colors">
                        <td className="px-4 py-2 text-slate-300 font-medium">
                          {key ? METHOD_LABELS[key] : row.Method}
                          {i === 0 && (
                            <span className="ml-2 px-1.5 py-0.5 rounded bg-emerald-900/40 border border-emerald-700/40 text-emerald-300 text-[10px] font-semibold">
                              Best
                            </span>
                          )}
                        </td>
                        <td className="px-4 py-2 text-right text-slate-400 font-mono">{formatValue(row.MSE)}</td>
                        <td className="px-4 py-2 text-right text-slate-400 font-mono">{formatValue(row.MAE)}</td>
                        <td className="px-4 py-2 text-right text-slate-400 font-mono">{formatValue(row['Max Error'])}</td>
                        <td className="px-4 py-2 text-right text-slate-400 font-mono">{formatValue(row.Correlation)}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {seriesLoading && (
          <div className="flex items-center gap-2 text-xs text-slate-500 py-8 justify-center">
            <Loader2 className="w-4 h-4 animate-spin text-brand-400" />
            Loading spectra…
          </div>
        )}

        {series && domains && (
          <div className="space-y-3">
            <div className="flex items-center gap-4">
              <span className="text-[10px] font-semibold text-slate-600 uppercase tracking-widest">
                Spectra
              </span>
              {/* Legend: both panels' series named, so identity is never colour-alone. */}
              <span className="flex items-center gap-1.5 text-[11px] text-slate-500">
                <svg width="10" height="10" aria-hidden="true">
                  <circle cx="5" cy="5" r="4" fill="#e2e8f0" stroke="#0f1117" strokeWidth="2" />
                </svg>
                Measured
              </span>
              <span className="flex items-center gap-1.5 text-[11px] text-slate-500">
                <svg width="14" height="10" aria-hidden="true">
                  <line x1="0" y1="5" x2="14" y2="5" stroke={CURVE} strokeWidth="2" />
                </svg>
                Interpolated
              </span>
            </div>

            <div className="grid md:grid-cols-2 xl:grid-cols-3 gap-3">
              <SpectrumChart
                title="Measured data"
                subtitle={`${series.original_points} points`}
                x={[]}
                y={[]}
                originalX={series.original.x}
                originalY={series.original.y}
                color={CURVE}
                xDomain={domains.x}
                yDomain={domains.y}
                xLabel={snapshot.metadata.wavelength_column}
                yLabel={snapshot.metadata.coefficient_column}
              />

              {METHODS.filter((method) => series.methods[method]).map((method) => (
                <SpectrumChart
                  key={method}
                  title={METHOD_LABELS[method]}
                  subtitle={`${series.interpolated_points} points`}
                  x={series.wavelength}
                  y={series.methods[method] as number[]}
                  originalX={series.original.x}
                  originalY={series.original.y}
                  color={CURVE}
                  xDomain={domains.x}
                  yDomain={domains.y}
                  xLabel={snapshot.metadata.wavelength_column}
                  yLabel={snapshot.metadata.coefficient_column}
                  badge={
                    method === bestMethod ? (
                      <span className="px-1.5 py-0.5 rounded bg-emerald-900/40 border border-emerald-700/40 text-emerald-300 text-[9px] font-semibold">
                        Best
                      </span>
                    ) : null
                  }
                />
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

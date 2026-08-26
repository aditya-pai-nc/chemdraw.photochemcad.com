'use client'

import type { JSX } from 'react'
import { FileSpreadsheet, FolderArchive, RotateCcw, CheckCircle2, XCircle, AlertCircle } from 'lucide-react'
import type { CompoundRow } from '@/lib/types'
import { archiveUrl, excelUrl } from '@/lib/api'

interface Props {
  jobId: string | null
  compounds: CompoundRow[]
  errorMessage: string | null
  onReset: () => void
}

export function ResultsScreen({
  jobId, compounds, errorMessage, onReset
}: Props): JSX.Element {
  const matched = compounds.filter((c) => c.match === '✅').length
  const unmatched = compounds.filter((c) => c.match === '❌').length
  const total = compounds.length
  const matchRate = total > 0 ? Math.round((matched / total) * 100) : 0
  const success = !errorMessage && !!jobId

  return (
    <div className="flex flex-col h-full px-6 py-5 gap-5 overflow-y-auto">
      {success ? (
        <div className="flex items-center gap-3 px-4 py-3 rounded-xl bg-emerald-900/30 border border-emerald-700/50">
          <CheckCircle2 className="w-5 h-5 text-emerald-400 shrink-0" />
          <div>
            <p className="text-sm font-semibold text-emerald-300">Processing complete</p>
            <p className="text-xs text-emerald-600 mt-0.5">Download the Excel workbook or a ZIP of all generated files.</p>
          </div>
        </div>
      ) : (
        <div className="flex items-center gap-3 px-4 py-3 rounded-xl bg-red-900/30 border border-red-700/50">
          <AlertCircle className="w-5 h-5 text-red-400 shrink-0" />
          <div>
            <p className="text-sm font-semibold text-red-300">Processing failed</p>
            {errorMessage && <p className="text-xs text-red-500 mt-0.5 whitespace-pre-wrap">{errorMessage}</p>}
          </div>
        </div>
      )}

      {total > 0 && (
        <div className="grid grid-cols-3 gap-3 shrink-0">
          <div className="rounded-xl bg-slate-900 border border-slate-800 px-4 py-3 text-center">
            <p className="text-2xl font-bold text-white">{total}</p>
            <p className="text-xs text-slate-500 mt-0.5">Total Compounds</p>
          </div>
          <div className="rounded-xl bg-emerald-900/20 border border-emerald-800/40 px-4 py-3 text-center">
            <p className="text-2xl font-bold text-emerald-400">{matched}</p>
            <p className="text-xs text-emerald-600 mt-0.5">PubChem Matched</p>
          </div>
          <div className={`rounded-xl px-4 py-3 text-center border ${
            unmatched > 0 ? 'bg-red-900/20 border-red-800/40' : 'bg-slate-900 border-slate-800'
          }`}>
            <p className={`text-2xl font-bold ${unmatched > 0 ? 'text-red-400' : 'text-slate-500'}`}>{unmatched}</p>
            <p className="text-xs text-slate-500 mt-0.5">Unmatched</p>
          </div>
        </div>
      )}

      {total > 0 && (
        <div className="shrink-0 space-y-1">
          <div className="flex justify-between text-xs text-slate-500">
            <span>PubChem match rate</span>
            <span className="font-semibold text-slate-300">{matchRate}%</span>
          </div>
          <div className="h-2 rounded-full bg-slate-800 overflow-hidden">
            <div
              className="h-full rounded-full bg-gradient-to-r from-brand-600 to-emerald-500 transition-all duration-500"
              style={{ width: `${matchRate}%` }}
            />
          </div>
        </div>
      )}

      <div className="flex gap-3 shrink-0">
        {success && jobId && (
          <a
            href={excelUrl(jobId)}
            className="flex-1 flex items-center justify-center gap-2 px-4 py-3 rounded-xl bg-brand-600 hover:bg-brand-500 text-white text-sm font-semibold transition-colors"
          >
            <FileSpreadsheet className="w-4 h-4" />
            Download Excel
          </a>
        )}
        {success && jobId && (
          <a
            href={archiveUrl(jobId)}
            className="flex-1 flex items-center justify-center gap-2 px-4 py-3 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 text-sm font-semibold transition-colors"
          >
            <FolderArchive className="w-4 h-4" />
            Download ZIP
          </a>
        )}
        <button
          onClick={onReset}
          className="flex items-center justify-center gap-2 px-4 py-3 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-400 text-sm transition-colors"
        >
          <RotateCcw className="w-4 h-4" />
          New File
        </button>
      </div>

      {compounds.length > 0 && (
        <div className="flex-1 min-h-0 rounded-xl overflow-hidden border border-slate-800">
          <div className="px-4 py-2.5 bg-slate-900 border-b border-slate-800 flex items-center justify-between shrink-0">
            <span className="text-xs font-semibold text-slate-500 uppercase tracking-widest">Compounds</span>
          </div>
          <div className="overflow-y-auto max-h-72">
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-slate-900/90 backdrop-blur-sm">
                <tr>
                  <th className="text-left px-4 py-2 text-slate-500 font-medium w-8">#</th>
                  <th className="text-left px-4 py-2 text-slate-500 font-medium">Compound Name</th>
                  <th className="text-center px-4 py-2 text-slate-500 font-medium w-24">PubChem</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/50">
                {compounds.map((c, i) => (
                  <tr key={i} className="hover:bg-slate-800/30 transition-colors">
                    <td className="px-4 py-2 text-slate-600">{c.index}</td>
                    <td className="px-4 py-2 text-slate-300 font-medium">{c.name}</td>
                    <td className="px-4 py-2 text-center">
                      {c.match === '✅'
                        ? <span className="flex items-center justify-center gap-1 text-emerald-400"><CheckCircle2 className="w-3 h-3" />Match</span>
                        : <span className="flex items-center justify-center gap-1 text-red-400"><XCircle className="w-3 h-3" />No match</span>
                      }
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}

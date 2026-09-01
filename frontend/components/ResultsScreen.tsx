'use client'

import type { JSX } from 'react'
import { FileSpreadsheet, FolderArchive, RotateCcw, CheckCircle2, XCircle, AlertCircle, MinusCircle, CircleAlert } from 'lucide-react'
import type { CompoundRow, MatchSymbol } from '@/lib/types'
import { archiveUrl, excelUrl } from '@/lib/api'

/**
 * One verdict cell. A skeleton match ('🟡') is shown as its own state rather
 * than being rounded to a pass or a fail: it means the compound is right but the
 * stereochemistry was left undrawn, which is a drawing convention, not an error
 * — and lumping it in either direction is what the extra columns exist to avoid.
 */
function Verdict({ value, pending }: { value: MatchSymbol; pending?: boolean }): JSX.Element {
  if (pending) {
    return <span className="flex items-center justify-center text-slate-600">·&thinsp;·&thinsp;·</span>
  }
  switch (value) {
    case '✅':
      return <span className="flex items-center justify-center text-emerald-400" title="Match"><CheckCircle2 className="w-3.5 h-3.5" /></span>
    case '🟡':
      return <span className="flex items-center justify-center text-amber-400" title="Same skeleton, different stereochemistry or protonation"><CircleAlert className="w-3.5 h-3.5" /></span>
    case '❌':
      return <span className="flex items-center justify-center text-red-400" title="No match"><XCircle className="w-3.5 h-3.5" /></span>
    default:
      return <span className="flex items-center justify-center text-slate-600" title="Nothing to compare"><MinusCircle className="w-3.5 h-3.5" /></span>
  }
}

interface Props {
  jobId: string | null
  compounds: CompoundRow[]
  errorMessage: string | null
  onReset: () => void
}

export function ResultsScreen({
  jobId, compounds, errorMessage, onReset
}: Props): JSX.Element {
  const total = compounds.length
  const matched = compounds.filter((c) => c.match === '✅').length
  const keyMatched = compounds.filter((c) => c.inchikeyMatch === '✅').length
  const keyPartial = compounds.filter((c) => c.inchikeyMatch === '🟡').length
  const aiMatched = compounds.filter((c) => c.aiMatch === '✅').length
  const aiRan = compounds.filter((c) => c.aiDone).length
  // The reason for reporting the InChIKey column separately: how many compounds
  // the structure hash identified that formula-and-weight alone did not.
  const keyOnlyWins = compounds.filter((c) => c.match !== '✅' && c.inchikeyMatch === '✅').length
  const rate = (n: number) => (total > 0 ? Math.round((n / total) * 100) : 0)
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
        <div className="grid grid-cols-4 gap-3 shrink-0">
          <div className="rounded-xl bg-slate-900 border border-slate-800 px-4 py-3 text-center">
            <p className="text-2xl font-bold text-white">{total}</p>
            <p className="text-xs text-slate-500 mt-0.5">Compounds</p>
          </div>
          <div className="rounded-xl bg-slate-900 border border-slate-800 px-4 py-3 text-center">
            <p className="text-2xl font-bold text-slate-200">{matched}</p>
            <p className="text-xs text-slate-500 mt-0.5">Formula match</p>
            <p className="text-[10px] text-slate-600">{rate(matched)}%</p>
          </div>
          <div className="rounded-xl bg-emerald-900/20 border border-emerald-800/40 px-4 py-3 text-center">
            <p className="text-2xl font-bold text-emerald-400">
              {keyMatched}
              {keyPartial > 0 && <span className="text-base text-amber-400"> +{keyPartial}</span>}
            </p>
            <p className="text-xs text-emerald-600 mt-0.5">InChIKey match</p>
            <p className="text-[10px] text-slate-600">
              {rate(keyMatched)}%{keyPartial > 0 && ` · ${keyPartial} skeleton`}
            </p>
          </div>
          <div className="rounded-xl bg-violet-900/20 border border-violet-800/40 px-4 py-3 text-center">
            <p className="text-2xl font-bold text-violet-300">{aiRan > 0 ? aiMatched : '—'}</p>
            <p className="text-xs text-violet-500/80 mt-0.5">AI match</p>
            <p className="text-[10px] text-slate-600">
              {aiRan > 0 ? `${rate(aiMatched)}% of ${aiRan} run` : 'AI pass not run'}
            </p>
          </div>
        </div>
      )}

      {total > 0 && (
        <div className="shrink-0 space-y-2">
          <div className="flex justify-between text-xs text-slate-500">
            <span>Identification rate</span>
            <span className="font-semibold text-slate-300">
              {rate(keyMatched + keyPartial)}% by InChIKey · {rate(matched)}% by formula
            </span>
          </div>
          <div className="h-2 rounded-full bg-slate-800 overflow-hidden flex">
            <div
              className="h-full bg-emerald-500 transition-all duration-500"
              style={{ width: `${rate(keyMatched)}%` }}
              title={`${keyMatched} exact InChIKey matches`}
            />
            <div
              className="h-full bg-amber-500 transition-all duration-500"
              style={{ width: `${rate(keyPartial)}%` }}
              title={`${keyPartial} matched on skeleton only`}
            />
          </div>
          {keyOnlyWins > 0 && (
            <p className="text-xs text-emerald-500/80">
              The InChIKey found {keyOnlyWins} compound{keyOnlyWins === 1 ? '' : 's'} that
              formula and weight alone did not.
            </p>
          )}
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
            <span className="text-[10px] text-slate-600">
              Manual review column is in the workbook
            </span>
          </div>
          <div className="overflow-y-auto max-h-72">
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-slate-900/90 backdrop-blur-sm">
                <tr>
                  <th className="text-left px-4 py-2 text-slate-500 font-medium w-8">#</th>
                  <th className="text-left px-4 py-2 text-slate-500 font-medium">Compound Name</th>
                  <th className="text-center px-3 py-2 text-slate-500 font-medium w-20" title="PubChem formula and weight">Formula</th>
                  <th className="text-center px-3 py-2 text-slate-500 font-medium w-20" title="PubChem InChIKey — a hash of the structure">InChIKey</th>
                  <th className="text-center px-3 py-2 text-slate-500 font-medium w-20" title="Claude's independent identification, against the drawn structure">AI</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/50">
                {compounds.map((c, i) => (
                  <tr key={i} className="hover:bg-slate-800/30 transition-colors">
                    <td className="px-4 py-2 text-slate-600">{c.index}</td>
                    <td className="px-4 py-2 text-slate-300 font-medium">{c.name}</td>
                    <td className="px-3 py-2"><Verdict value={c.match} /></td>
                    <td className="px-3 py-2"><Verdict value={c.inchikeyMatch} /></td>
                    <td className="px-3 py-2"><Verdict value={c.aiMatch} pending={!c.aiDone} /></td>
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

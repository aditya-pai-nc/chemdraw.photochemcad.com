'use client'

import type { JSX } from 'react'
import { FileSpreadsheet, FolderArchive, RotateCcw, CheckCircle2, XCircle, AlertCircle, MinusCircle } from 'lucide-react'
import type { CompoundRow, MatchSymbol } from '@/lib/types'
import { archiveUrl, excelUrl } from '@/lib/api'
import { CasVerification } from './CasVerification'

/** One verdict cell: agree, disagree, or nothing to compare. */
function Verdict({ value }: { value: MatchSymbol }): JSX.Element {
  switch (value) {
    case '✅':
      return <span className="flex items-center justify-center text-emerald-400" title="Match"><CheckCircle2 className="w-3.5 h-3.5" /></span>
    case '❌':
      return <span className="flex items-center justify-center text-red-400" title="No match"><XCircle className="w-3.5 h-3.5" /></span>
    default:
      return <span className="flex items-center justify-center text-slate-600" title="Nothing to compare"><MinusCircle className="w-3.5 h-3.5" /></span>
  }
}

/**
 * The curator's verdict, shown only for compounds that did not match exactly.
 * A matched compound never reaches curation, so its cell says so rather than
 * showing an empty state that reads like a failure.
 */
function Curated({
  verdict, matched
}: { verdict?: 'yes' | 'no' | 'uncertain' | null; matched: boolean }): JSX.Element {
  if (matched) {
    return <span className="flex items-center justify-center text-slate-700 text-[10px]">not needed</span>
  }
  if (!verdict) {
    return <span className="flex items-center justify-center text-slate-600">·&thinsp;·&thinsp;·</span>
  }
  const style = {
    yes: ['text-emerald-400', 'same compound'],
    no: ['text-red-400', 'differs'],
    uncertain: ['text-amber-400', 'uncertain']
  }[verdict]
  return (
    <span className={`flex items-center justify-center text-[10px] ${style[0]}`} title={style[1]}>
      {style[1]}
    </span>
  )
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
  const formulaMatched = compounds.filter((c) => c.match === '✅').length
  const matched = compounds.filter((c) => c.inchikeyMatch === '✅').length
  const unmatched = total - matched
  const casVerified = compounds.filter((c) => c.casVerification === 'Verified').length
  const casCompared = compounds.filter((c) => c.casVerification === 'Verified' || c.casVerification === 'Mismatch').length
  const curated = compounds.filter((c) => c.curated).length
  // Of the curated ones, how many the model still judged to be the same
  // compound — a salt form or a tautomer is the usual reason.
  const curatedSame = compounds.filter((c) => c.curated === 'yes').length
  // The reason for reporting the structural column separately: how many
  // compounds canonical matching confirmed that formula-and-weight alone did not.
  const structureOnlyWins = compounds.filter((c) => c.match !== '✅' && c.inchikeyMatch === '✅').length
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
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3 shrink-0">
          <div className="rounded-xl bg-slate-900 border border-slate-800 px-4 py-3 text-center">
            <p className="text-2xl font-bold text-white">{total}</p>
            <p className="text-xs text-slate-500 mt-0.5">Compounds</p>
          </div>
          <div className="rounded-xl bg-slate-900 border border-slate-800 px-4 py-3 text-center">
            <p className="text-2xl font-bold text-slate-200">{formulaMatched}</p>
            <p className="text-xs text-slate-500 mt-0.5">Formula match</p>
            <p className="text-[10px] text-slate-600">{rate(formulaMatched)}%</p>
          </div>
          <div className="rounded-xl bg-emerald-900/20 border border-emerald-800/40 px-4 py-3 text-center">
            <p className="text-2xl font-bold text-emerald-400">{matched}</p>
            <p className="text-xs text-emerald-600 mt-0.5">InChIKey match</p>
            <p className="text-[10px] text-slate-600">{rate(matched)}%</p>
          </div>
          <div className="rounded-xl bg-slate-900 border border-slate-800 px-4 py-3 text-center">
            <p className="text-2xl font-bold text-emerald-400">{casCompared > 0 ? casVerified : '—'}</p>
            <p className="text-xs text-slate-500 mt-0.5">CAS verified</p>
            <p className="text-[10px] text-slate-600">{casCompared > 0 ? `of ${casCompared} compared` : 'No comparable results'}</p>
          </div>
          <div className="rounded-xl bg-violet-900/20 border border-violet-800/40 px-4 py-3 text-center">
            <p className="text-2xl font-bold text-violet-300">{curated > 0 ? curated : '—'}</p>
            <p className="text-xs text-violet-500/80 mt-0.5">Curated</p>
            <p className="text-[10px] text-slate-600">
              {curated > 0
                ? `of ${unmatched} unmatched · ${curatedSame} same compound`
                : unmatched > 0 ? 'curation not run' : 'nothing to curate'}
            </p>
          </div>
        </div>
      )}

      {total > 0 && (
        <div className="shrink-0 space-y-2">
          <div className="flex justify-between text-xs text-slate-500">
            <span>Identification rate</span>
            <span className="font-semibold text-slate-300">
              {rate(matched)}% by structure · {rate(formulaMatched)}% by formula
            </span>
          </div>
          <div className="h-2 rounded-full bg-slate-800 overflow-hidden flex">
            <div
              className="h-full bg-emerald-500 transition-all duration-500"
              style={{ width: `${rate(matched)}%` }}
              title={`${matched} exact structural matches`}
            />
          </div>
          {structureOnlyWins > 0 && (
            <p className="text-xs text-emerald-500/80">
              The canonical SMILES check confirmed {structureOnlyWins} compound
              {structureOnlyWins === 1 ? '' : 's'} that formula and weight alone did not.
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
              Curated rows are on the workbook&apos;s second sheet
            </span>
          </div>
          <div className="overflow-auto max-h-72">
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-slate-900/90 backdrop-blur-sm">
                <tr>
                  <th className="text-left px-4 py-2 text-slate-500 font-medium w-8">#</th>
                  <th className="text-left px-4 py-2 text-slate-500 font-medium">Compound Name</th>
                  <th className="text-center px-3 py-2 text-slate-500 font-medium w-20" title="PubChem formula and weight">Formula</th>
                  <th className="text-center px-3 py-2 text-slate-500 font-medium w-20" title="Canonical SMILES from ChemDraw vs from PubChem">InChIKey Match</th>
                  <th className="text-center px-3 py-2 text-slate-500 font-medium w-28" title="Independent structure verification against CAS Common Chemistry">CAS Common Chemistry</th>
                  <th className="text-center px-3 py-2 text-slate-500 font-medium w-24" title="Curator's verdict for compounds that did not match exactly">Curated</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/50">
                {compounds.map((c, i) => (
                  <tr key={i} className="hover:bg-slate-800/30 transition-colors">
                    <td className="px-4 py-2 text-slate-600">{c.index}</td>
                    <td className="px-4 py-2 text-slate-300 font-medium">{c.name}</td>
                    <td className="px-3 py-2"><Verdict value={c.match} /></td>
                    <td className="px-3 py-2"><Verdict value={c.inchikeyMatch} /></td>
                    <td className="px-3 py-2"><CasVerification result={c} /></td>
                    <td className="px-3 py-2"><Curated verdict={c.curated} matched={c.inchikeyMatch === '✅'} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      {compounds.some((c) => c.casRn) && (
        <p className="text-[10px] text-slate-500 shrink-0">
          CAS data: <a href="https://commonchemistry.cas.org/" target="_blank" rel="noopener noreferrer" className="underline">CAS Common Chemistry</a>,
          {' '}CAS, a division of the American Chemical Society.{' '}
          <a href="https://creativecommons.org/licenses/by-nc/4.0/" target="_blank" rel="noopener noreferrer" className="underline">CC BY-NC 4.0</a>.
          {' '}Comparison details and reference values are included in the workbook.
        </p>
      )}
    </div>
  )
}

'use client'

import { Fragment, useEffect, useRef, useState } from 'react'
import { ArrowDownToLine, ArrowRight, ChevronDown, ChevronRight, ExternalLink, FileSpreadsheet, ListFilter, Loader2, Upload, X } from 'lucide-react'
import { bulkDownload, cancelBulkJob, createBulkJob, finished, getBulkJob, type BulkJob, type ChemicalRecord } from '@/lib/bulk-cas'

const STORAGE_KEY = 'chemdraw:bulkCasJob'
const FORMATS = /\.(txt|csv|tsv|xlsx|xls)$/i

function remember(id: string | null) {
  try {
    if (id) window.localStorage.setItem(STORAGE_KEY, id)
    else window.localStorage.removeItem(STORAGE_KEY)
  } catch { /* The batch still works when local storage is unavailable. */ }
}

function SourceRecord({ title, record }: { title: string; record: ChemicalRecord }) {
  return (
    <section className="min-w-0 rounded-lg border border-slate-800 bg-[#0f1117] p-4">
      <div className="flex justify-between gap-3 mb-3">
        <h3 className="font-semibold text-slate-200">{title}</h3>
        <span className="text-slate-500">{record.status}</span>
      </div>
      {record.status === 'Found' && (
        <dl className="space-y-2.5">
          {([
            ['Name', record.name], ['CAS RN', record.rn], ['PubChem CID', record.cid],
            ['Molecular formula', record.formula], ['Molecular weight', record.weight],
            ['SMILES', record.smiles], ['InChI', record.inchi], ['InChIKey', record.inchikey]
          ] as const).filter(([label, value]) => value != null || !['CAS RN', 'PubChem CID'].includes(label)).map(([label, value]) => (
            <div key={label}>
              <dt className="text-[10px] uppercase tracking-wide text-slate-500 mb-0.5">{label}</dt>
              <dd className="font-mono text-slate-300 break-all select-text">{value ?? 'Not supplied'}</dd>
            </div>
          ))}
        </dl>
      )}
      {record.note && <p className="text-slate-400 mt-3 leading-relaxed">{record.note}</p>}
      {record.url && <a href={record.url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1.5 text-brand-300 mt-4 hover:underline">Open source record <ExternalLink className="w-3 h-3" /></a>}
    </section>
  )
}

export default function BulkCasPage() {
  const [file, setFile] = useState<File | null>(null)
  const [dragging, setDragging] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [resuming, setResuming] = useState(true)
  const [jobId, setJobId] = useState<string | null>(null)
  const [job, setJob] = useState<BulkJob | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [openRow, setOpenRow] = useState<number | null>(null)
  const [reviewOnly, setReviewOnly] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const input = useRef<HTMLInputElement>(null)

  useEffect(() => {
    try { setJobId(window.localStorage.getItem(STORAGE_KEY)) } catch { /* optional */ }
    setResuming(false)
  }, [])

  useEffect(() => {
    if (!jobId) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const result = await getBulkJob(jobId, controller.signal)
        if (controller.signal.aborted) return
        setJob(result)
        setError(null)
        if (!finished(result)) timer = setTimeout(poll, 1500)
      } catch (err) {
        if (controller.signal.aborted) return
        if ((err as { status?: number }).status === 404) {
          remember(null)
          setJobId(null)
          setJob(null)
          setError('The previous batch is no longer available. Please upload your list again.')
        } else {
          setError('Connection interrupted. Reconnecting to your batch…')
          timer = setTimeout(poll, 3000)
        }
      }
    }
    void poll()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [jobId])

  const selectFile = (candidate: File | undefined) => {
    if (!candidate) return
    if (!FORMATS.test(candidate.name) || candidate.size > 2 * 1024 * 1024 || candidate.size === 0) {
      setError('Choose a nonempty TXT, CSV, TSV, XLSX or XLS file no larger than 2 MB.')
      return
    }
    setError(null)
    setFile(candidate)
  }

  const start = async () => {
    if (!file || uploading) return
    setUploading(true)
    setError(null)
    try {
      const result = await createBulkJob(file)
      setJob(result)
      setJobId(result.job_id)
      remember(result.job_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not upload this file.')
    } finally { setUploading(false) }
  }

  const cancel = async () => {
    if (!jobId) return
    setCancelling(true)
    try { setJob(await cancelBulkJob(jobId)) }
    catch (err) { setError(err instanceof Error ? err.message : 'Could not stop this batch.') }
    finally { setCancelling(false) }
  }

  const reset = () => {
    remember(null)
    setJobId(null)
    setJob(null)
    setFile(null)
    setError(null)
    setOpenRow(null)
    setReviewOnly(false)
  }

  const busy = job != null && !finished(job)
  const found = job?.rows.filter(row => row.status === 'Found').length ?? 0
  const needsReview = job?.rows.filter(row => row.status !== 'Found' || row.agreement === 'Differ').length ?? 0
  const rows = job?.rows.filter(row => !reviewOnly || row.status !== 'Found' || row.agreement === 'Differ') ?? []

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-7xl mx-auto px-5 sm:px-8 py-8 space-y-6">
        <header className="flex flex-wrap justify-between items-start gap-4">
          <div>
            <p className="text-[10px] text-brand-300 font-semibold uppercase tracking-[0.2em] mb-2">PhotochemCAD tools</p>
            <h1 className="text-2xl font-semibold tracking-tight text-white">Bulk chemical information extractor</h1>
            <p className="text-sm text-slate-400 mt-2 max-w-2xl leading-relaxed">Turn a list of CAS numbers into chemical data from PubChem and CAS Common Chemistry.</p>
          </div>
          {job && !busy && <button onClick={reset} className="px-4 py-2 border border-slate-700 rounded-lg text-xs text-slate-300 hover:bg-slate-800">New list</button>}
        </header>

        {(error || job?.error) && <div role="alert" className="rounded-lg border border-amber-700/40 bg-amber-900/15 p-4 text-sm text-amber-200">{error || job?.error}</div>}

        {!job && (resuming || jobId) ? <p className="text-slate-400 flex items-center gap-2"><Loader2 className="w-4 h-4 animate-spin" />Loading your batch…</p> : !job && (
          <div className="grid lg:grid-cols-[1.3fr_1fr] gap-6">
            <section className="rounded-2xl border border-slate-800 bg-slate-900/40 p-6 space-y-5">
              <div className="flex items-center gap-2 text-sm font-semibold text-slate-200"><Upload className="w-4 h-4 text-brand-300" />Upload your CAS list</div>
              <input ref={input} id="cas-file" type="file" accept=".txt,.csv,.tsv,.xlsx,.xls" className="sr-only" disabled={uploading} onChange={event => selectFile(event.target.files?.[0])} />
              <button type="button" disabled={uploading} onClick={() => input.current?.click()}
                onDragOver={event => { event.preventDefault(); setDragging(true) }}
                onDragLeave={() => setDragging(false)}
                onDrop={event => { event.preventDefault(); setDragging(false); if (!uploading) selectFile(event.dataTransfer.files[0]) }}
                className={`w-full min-h-52 border border-dashed rounded-xl flex flex-col items-center justify-center gap-3 px-5 transition-colors ${dragging ? 'border-brand-400 bg-brand-600/10' : 'border-slate-700 hover:border-slate-500 bg-[#0f1117]'}`}>
                <FileSpreadsheet className="w-9 h-9 text-slate-500" />
                <span className="text-sm text-slate-200 break-all">{file ? file.name : 'Drop a file here, or browse'}</span>
                <span className="text-xs text-slate-500">{file ? `${(file.size / 1024).toFixed(1)} KB · Click to replace` : 'TXT, CSV, TSV, XLSX or XLS · Up to 2 MB'}</span>
              </button>
              <p className="text-xs text-slate-500 leading-relaxed">One CAS number per line, or a spreadsheet column headed <span className="font-mono text-slate-300">CAS RN</span>. Supports lists of 100 numbers and up to 500 entries.</p>
              <button onClick={start} disabled={!file || uploading} className="w-full flex items-center justify-center gap-2 py-3 rounded-lg bg-brand-600 text-white text-sm font-semibold hover:bg-brand-500 disabled:opacity-40 disabled:cursor-not-allowed">
                {uploading ? <Loader2 className="w-4 h-4 animate-spin" /> : <ArrowRight className="w-4 h-4" />}{uploading ? 'Reading your list…' : 'Extract chemical data'}
              </button>
            </section>
            <aside className="rounded-2xl border border-slate-800 p-6 flex flex-col justify-between gap-7">
              <div>
                <h2 className="text-sm font-semibold text-slate-200 mb-4">From CAS numbers to a ready-to-use table</h2>
                <ul className="text-sm text-slate-400 space-y-3 leading-relaxed">
                  <li>Molecular formula and molecular weight</li>
                  <li>SMILES, InChI and InChIKey</li>
                  <li>Direct PubChem and CAS Common Chemistry links</li>
                  <li>Excel and CSV downloads, with source details</li>
                </ul>
                <p className="text-xs text-slate-500 leading-relaxed mt-5">Invalid numbers stay in the results for correction. Duplicate entries keep their place in your list.</p>
              </div>
              <div className="border-t border-slate-800 pt-4">
                <p className="font-mono text-xs leading-6 text-slate-400">CAS RN<br />50-78-2<br />58-08-2<br />64-17-5</p>
                <a href="/api/bulk-cas/sample.csv" className="inline-flex items-center gap-2 text-xs text-brand-300 mt-3 hover:underline"><ArrowDownToLine className="w-3.5 h-3.5" />Download sample list</a>
              </div>
            </aside>
          </div>
        )}

        {job && <>
          <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-5 space-y-4" aria-live="polite">
            <div className="flex justify-between items-center gap-4">
              <div className="min-w-0">
                <h2 className="text-sm font-semibold text-slate-200 flex items-center gap-2">
                  {busy && <Loader2 className="w-4 h-4 animate-spin text-brand-300" />}
                  {job.cancel_requested && busy ? 'Stopping after the current lookup…' : job.status === 'queued' ? 'Waiting for the extractor' : job.status === 'running' ? job.processed === job.total ? 'Preparing downloads…' : 'Retrieving chemical data' : job.status === 'done' ? 'Extraction complete' : job.status === 'cancelled' ? 'Extraction stopped' : 'Extraction interrupted'}
                </h2>
                <p className="text-xs text-slate-500 mt-1 truncate">{job.filename} · {job.unique} unique valid CAS numbers</p>
              </div>
              {busy && <button onClick={cancel} disabled={cancelling || job.cancel_requested} className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg border border-slate-700 text-xs text-slate-400 hover:text-white disabled:opacity-40"><X className="w-3.5 h-3.5" />Stop</button>}
            </div>
            <div className="h-1.5 rounded bg-slate-800 overflow-hidden" role="progressbar" aria-label="CAS entries processed" aria-valuemin={0} aria-valuemax={job.total} aria-valuenow={job.processed}>
              <div className="h-full bg-brand-500 transition-all" style={{ width: `${job.total ? job.processed / job.total * 100 : 0}%` }} />
            </div>
            <div className="flex flex-wrap justify-between gap-2 text-xs text-slate-400"><span>{job.processed} / {job.total} entries processed</span><span><span className="text-emerald-400">{found} found</span> · {needsReview} need review</span></div>
          </section>

          <div className="flex flex-wrap items-center justify-between gap-3">
            <button onClick={() => setReviewOnly(value => !value)} aria-pressed={reviewOnly} className={`inline-flex items-center gap-2 px-3 py-2 text-xs border rounded-lg ${reviewOnly ? 'text-amber-300 border-amber-600/50 bg-amber-900/10' : 'text-slate-400 border-slate-700'}`}><ListFilter className="w-3.5 h-3.5" />{reviewOnly ? 'Showing entries to review' : 'Show entries to review'}</button>
            <div className="flex gap-2">
              {job.has_exports && <><a href={bulkDownload(job.job_id, 'csv')} className="px-4 py-2 rounded-lg border border-slate-700 text-xs text-slate-300 hover:bg-slate-800">Download CSV</a><a href={bulkDownload(job.job_id, 'xlsx')} className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-brand-600 hover:bg-brand-500 text-xs font-semibold text-white"><ArrowDownToLine className="w-3.5 h-3.5" />Download Excel</a></>}
            </div>
          </div>

          <section className="rounded-xl border border-slate-800 overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full text-xs text-left">
                <thead className="bg-slate-900 text-slate-500"><tr>{['CAS number', 'Name', 'Formula', 'Mol. weight', 'Data source', 'Status', 'Records'].map(label => <th key={label} className="px-4 py-3 font-medium whitespace-nowrap">{label}</th>)}</tr></thead>
                <tbody className="divide-y divide-slate-800/70">
                  {rows.map(row => <Fragment key={row.index}>
                    <tr className="hover:bg-slate-800/20">
                      <td className="px-4 py-3"><button onClick={() => setOpenRow(openRow === row.index ? null : row.index)} aria-expanded={openRow === row.index} aria-label={`Details for entry ${row.index}: ${row.input}`} className="inline-flex items-center gap-2 font-mono text-slate-200 whitespace-nowrap">{openRow === row.index ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}{row.input}</button></td>
                      <td className="px-4 py-3 text-slate-300 min-w-40 max-w-64 break-words">{row.name ?? '—'}</td>
                      <td className="px-4 py-3 font-mono text-slate-300 whitespace-nowrap">{row.formula ?? '—'}</td>
                      <td className="px-4 py-3 font-mono text-slate-300">{row.weight ?? '—'}</td>
                      <td className="px-4 py-3 text-slate-400">{row.source ?? '—'}</td>
                      <td className={`px-4 py-3 whitespace-nowrap ${row.status === 'Found' && row.agreement !== 'Differ' ? 'text-emerald-400' : 'text-amber-300'}`}>{row.agreement === 'Differ' ? 'Sources differ' : row.status}</td>
                      <td className="px-4 py-3 whitespace-nowrap"><div className="flex gap-3">{row.pubchem.url && <a href={row.pubchem.url} target="_blank" rel="noopener noreferrer" className="text-brand-300 hover:underline">PubChem ↗</a>}{row.cas.url && <a href={row.cas.url} target="_blank" rel="noopener noreferrer" className="text-brand-300 hover:underline">CAS ↗</a>}{!row.pubchem.url && !row.cas.url && <span className="text-slate-600">—</span>}</div></td>
                    </tr>
                    {openRow === row.index && <tr><td colSpan={7} className="px-5 py-4 bg-slate-900/30">
                      <p className="text-slate-500 mb-3">Entry {row.index} · {row.location} · Structure agreement: {row.agreement}</p>
                      {row.notes && <p className="text-amber-200/80 mb-4 leading-relaxed">{row.notes}</p>}
                      <div className="grid md:grid-cols-2 gap-4"><SourceRecord title="CAS Common Chemistry" record={row.cas} /><SourceRecord title="PubChem" record={row.pubchem} /></div>
                    </td></tr>}
                  </Fragment>)}
                  {rows.length === 0 && <tr><td colSpan={7} className="p-8 text-center text-slate-500">{job.processed === 0 && busy ? 'Results will appear as each CAS number is processed.' : reviewOnly ? 'No processed entries need review.' : 'No entries were processed.'}</td></tr>}
                </tbody>
              </table>
            </div>
          </section>
          <p className="text-xs text-slate-500 leading-relaxed">Expand a CAS number to view SMILES, InChI and both source records. The summary uses CAS Common Chemistry when a record is available, otherwise PubChem. Missing values remain blank; Excel includes all source values.</p>
        </>}

        <footer className="text-[10px] text-slate-500 border-t border-slate-800 pt-4 leading-relaxed">
          Sources: <a href="https://pubchem.ncbi.nlm.nih.gov/" target="_blank" rel="noopener noreferrer" className="underline">PubChem</a> and <a href="https://commonchemistry.cas.org/" target="_blank" rel="noopener noreferrer" className="underline">CAS Common Chemistry</a>, CAS, a division of the American Chemical Society. CAS data: <a href="https://creativecommons.org/licenses/by-nc/4.0/" target="_blank" rel="noopener noreferrer" className="underline">CC BY-NC 4.0</a>.
        </footer>
      </div>
    </div>
  )
}

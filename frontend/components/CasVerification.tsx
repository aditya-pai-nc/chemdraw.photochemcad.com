import type { JSX } from 'react'
import type { CasResult } from '@/lib/types'

export function CasVerification({ result }: { result: CasResult }): JSX.Element {
  const status = result.casVerification ?? 'Not checked'
  const color = status === 'Verified' ? 'text-emerald-400' :
    status === 'Mismatch' ? 'text-red-400' : 'text-slate-400'
  return (
    <div className={`text-center text-[10px] ${color}`} title={result.casDetail ?? status}>
      <span>{status}</span>
      {result.casLink && result.casRn && (
        <a href={result.casLink} target="_blank" rel="noopener noreferrer"
          className="block underline underline-offset-2 hover:text-white"
          aria-label={`CAS Common Chemistry record ${result.casRn}`}>
          {result.casRn}
        </a>
      )}
    </div>
  )
}

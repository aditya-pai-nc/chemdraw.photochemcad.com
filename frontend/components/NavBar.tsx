'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import type { JSX } from 'react'
import { FlaskConical, ChartSpline } from 'lucide-react'

const TABS = [
  { href: '/', label: 'ChemDraw Processor', icon: FlaskConical },
  { href: '/interpolation', label: 'Interpolation', icon: ChartSpline }
]

export function NavBar(): JSX.Element {
  const pathname = usePathname()

  return (
    <nav className="flex items-center gap-1 px-4 py-2.5 border-b border-slate-800 shrink-0 select-none">
      <div className="flex items-center gap-2.5 pr-4 mr-2 border-r border-slate-800">
        <div className="w-2.5 h-2.5 rounded-full bg-brand-500 shadow-[0_0_8px_theme(colors.brand.500)]" />
        <span className="text-sm font-semibold tracking-wide text-slate-300">PhotochemCAD</span>
      </div>

      {TABS.map(({ href, label, icon: Icon }) => {
        const active = href === '/' ? pathname === '/' : pathname.startsWith(href)
        return (
          <Link
            key={href}
            href={href}
            className={`flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
              active
                ? 'bg-brand-600/20 text-brand-300 border border-brand-500/30'
                : 'text-slate-500 hover:text-slate-300 hover:bg-slate-800/60 border border-transparent'
            }`}
          >
            <Icon className="w-3.5 h-3.5" />
            {label}
          </Link>
        )
      })}
    </nav>
  )
}

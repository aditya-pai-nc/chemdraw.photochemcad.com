import type { Metadata } from 'next'
import type { ReactNode } from 'react'
import { NavBar } from '@/components/NavBar'
import './globals.css'

export const metadata: Metadata = {
  title: 'PhotochemCAD Tools',
  description:
    'Upload a ChemDraw file (.cdx) to extract molecular structures, generate SMILES, and enrich with PubChem data.'
}

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        <link
          href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap"
          rel="stylesheet"
        />
      </head>
      <body>
        <div id="root" className="flex flex-col bg-[#0f1117] text-slate-200">
          <NavBar />
          <main className="flex-1 min-h-0">{children}</main>
        </div>
      </body>
    </html>
  )
}

'use client'

import { useCallback, useEffect, useMemo, useRef, useState, type JSX } from 'react'

const HEIGHT = 190
const PAD = { top: 10, right: 12, bottom: 26, left: 46 }
const SURFACE = '#0f1117'

function useElementWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T | null>(null)
  const [width, setWidth] = useState(0)

  useEffect(() => {
    const node = ref.current
    if (!node) return
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) setWidth(entry.contentRect.width)
    })
    observer.observe(node)
    setWidth(node.getBoundingClientRect().width)
    return () => observer.disconnect()
  }, [])

  return [ref, width]
}

/** Spectra span tiny residuals to ~1, so pick the notation per magnitude. */
export function formatValue(value: number): string {
  if (!Number.isFinite(value)) return '—'
  const magnitude = Math.abs(value)
  if (magnitude !== 0 && (magnitude < 1e-3 || magnitude >= 1e6)) return value.toExponential(2)
  return String(Number(value.toFixed(4)))
}

function ticks(min: number, max: number, count: number): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max) || min === max) return [min]
  return Array.from({ length: count + 1 }, (_, i) => min + ((max - min) * i) / count)
}

interface Props {
  title: string
  /** Named in the title, so a single-series panel needs no legend box. */
  subtitle?: string
  /** Interpolated curve. Empty renders a measured-points-only panel. */
  x: number[]
  y: number[]
  originalX: number[]
  originalY: number[]
  color: string
  xDomain: [number, number]
  yDomain: [number, number]
  xLabel: string
  yLabel: string
  badge?: JSX.Element | null
}

export function SpectrumChart({
  title, subtitle, x, y, originalX, originalY, color, xDomain, yDomain, xLabel, yLabel, badge
}: Props): JSX.Element {
  const [ref, width] = useElementWidth<HTMLDivElement>()
  const [hoverIndex, setHoverIndex] = useState<number | null>(null)

  const plotW = Math.max(0, width - PAD.left - PAD.right)
  const plotH = HEIGHT - PAD.top - PAD.bottom

  const scaleX = useCallback(
    (value: number) => {
      const [lo, hi] = xDomain
      if (hi === lo) return PAD.left + plotW / 2
      return PAD.left + ((value - lo) / (hi - lo)) * plotW
    },
    [xDomain, plotW]
  )

  const scaleY = useCallback(
    (value: number) => {
      const [lo, hi] = yDomain
      if (hi === lo) return PAD.top + plotH / 2
      return PAD.top + plotH - ((value - lo) / (hi - lo)) * plotH
    },
    [yDomain, plotH]
  )

  const path = useMemo(() => {
    if (plotW <= 0 || x.length === 0) return ''
    return x
      .map((value, i) => `${i === 0 ? 'M' : 'L'}${scaleX(value).toFixed(2)},${scaleY(y[i]).toFixed(2)}`)
      .join(' ')
  }, [x, y, scaleX, scaleY, plotW])

  const handleMove = (event: React.MouseEvent<SVGSVGElement>): void => {
    if (x.length === 0 || plotW <= 0) return
    const rect = event.currentTarget.getBoundingClientRect()
    const [lo, hi] = xDomain
    const ratio = (event.clientX - rect.left - PAD.left) / plotW
    const target = lo + ratio * (hi - lo)
    let nearest = 0
    let bestDistance = Infinity
    for (let i = 0; i < x.length; i++) {
      const distance = Math.abs(x[i] - target)
      if (distance < bestDistance) {
        bestDistance = distance
        nearest = i
      }
    }
    setHoverIndex(nearest)
  }

  const yTicks = ticks(yDomain[0], yDomain[1], 3)
  const xTicks = ticks(xDomain[0], xDomain[1], 4)
  const hovered = hoverIndex !== null && hoverIndex < x.length ? hoverIndex : null

  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
      <div className="flex items-baseline gap-2 mb-1">
        <h4 className="text-xs font-semibold text-slate-300">{title}</h4>
        {badge}
        {subtitle && <span className="text-[10px] text-slate-500 ml-auto">{subtitle}</span>}
      </div>

      <div ref={ref} className="relative w-full">
        {width > 0 && (
          <svg
            width={width}
            height={HEIGHT}
            role="img"
            aria-label={`${title}: ${yLabel} against ${xLabel}`}
            onMouseMove={handleMove}
            onMouseLeave={() => setHoverIndex(null)}
          >
            {/* Recessive grid + axis labels */}
            {yTicks.map((tick) => (
              <g key={`y${tick}`}>
                <line
                  x1={PAD.left}
                  x2={PAD.left + plotW}
                  y1={scaleY(tick)}
                  y2={scaleY(tick)}
                  stroke="#1e293b"
                  strokeWidth={1}
                />
                <text
                  x={PAD.left - 6}
                  y={scaleY(tick)}
                  textAnchor="end"
                  dominantBaseline="middle"
                  fontSize={9}
                  fill="#64748b"
                  fontFamily="ui-monospace, monospace"
                >
                  {formatValue(tick)}
                </text>
              </g>
            ))}
            {xTicks.map((tick) => (
              <text
                key={`x${tick}`}
                x={scaleX(tick)}
                y={HEIGHT - 8}
                textAnchor="middle"
                fontSize={9}
                fill="#64748b"
                fontFamily="ui-monospace, monospace"
              >
                {Math.round(tick)}
              </text>
            ))}

            {/* Interpolated curve */}
            {path && <path d={path} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" />}

            {/* Measured points, ringed in the surface so they stay legible on the line */}
            {originalX.map((value, i) => (
              <circle
                key={`p${i}`}
                cx={scaleX(value)}
                cy={scaleY(originalY[i])}
                r={4}
                fill="#e2e8f0"
                stroke={SURFACE}
                strokeWidth={2}
              />
            ))}

            {/* Hover crosshair */}
            {hovered !== null && (
              <g pointerEvents="none">
                <line
                  x1={scaleX(x[hovered])}
                  x2={scaleX(x[hovered])}
                  y1={PAD.top}
                  y2={PAD.top + plotH}
                  stroke="#475569"
                  strokeWidth={1}
                  strokeDasharray="3 3"
                />
                <circle
                  cx={scaleX(x[hovered])}
                  cy={scaleY(y[hovered])}
                  r={4}
                  fill={color}
                  stroke={SURFACE}
                  strokeWidth={2}
                />
              </g>
            )}
          </svg>
        )}

        {hovered !== null && (
          <div
            className="absolute pointer-events-none px-2 py-1 rounded-md bg-slate-950 border border-slate-700 text-[10px] font-mono whitespace-nowrap shadow-lg"
            style={{
              left: Math.min(Math.max(scaleX(x[hovered]) + 10, 0), Math.max(0, width - 130)),
              top: PAD.top
            }}
          >
            <div className="text-slate-400">
              {xLabel} <span className="text-slate-200">{formatValue(x[hovered])}</span>
            </div>
            <div className="text-slate-400">
              {yLabel} <span className="text-slate-200">{formatValue(y[hovered])}</span>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

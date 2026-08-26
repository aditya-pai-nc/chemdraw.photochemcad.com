'use client'

import { useCallback, useEffect, useRef, useState, type JSX } from 'react'
import { InterpolationHome } from '@/components/InterpolationHome'
import {
  InterpolationProcessing,
  type CompoundProgress
} from '@/components/InterpolationProcessing'
import { InterpolationResults } from '@/components/InterpolationResults'
import type { StreamStatus } from '@/lib/api'
import {
  cancelInterpolation,
  createInterpolation,
  getCompoundSeries,
  getInterpolation,
  subscribeToInterpolation,
  type CompoundSeries,
  type InterpolationEvent,
  type InterpolationSnapshot
} from '@/lib/interpolation'

type Screen = 'home' | 'processing' | 'results'

const ACTIVE_KEY = 'chemdraw:activeInterpolation'

function readStored(): string | null {
  try {
    return window.localStorage.getItem(ACTIVE_KEY)
  } catch {
    return null
  }
}

function store(id: string | null): void {
  try {
    if (id) window.localStorage.setItem(ACTIVE_KEY, id)
    else window.localStorage.removeItem(ACTIVE_KEY)
  } catch {
    // storage disabled — resuming just won't be available
  }
}

export default function InterpolationPage(): JSX.Element {
  const [screen, setScreen] = useState<Screen>('home')
  const [file, setFile] = useState<File | null>(null)
  const [filename, setFilename] = useState('')
  const [stepSize, setStepSize] = useState(1.0)
  const [jobId, setJobId] = useState<string | null>(null)
  const [resuming, setResuming] = useState(false)

  const [currentStage, setCurrentStage] = useState(0)
  const [stageMessage, setStageMessage] = useState('')
  const [compounds, setCompounds] = useState<CompoundProgress[]>([])
  const [totalCompounds, setTotalCompounds] = useState(0)
  const [logs, setLogs] = useState<{ level: string; text: string }[]>([])
  const [streamStatus, setStreamStatus] = useState<StreamStatus>('closed')
  const [errorMessage, setErrorMessage] = useState<string | null>(null)

  const [snapshot, setSnapshot] = useState<InterpolationSnapshot | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [series, setSeries] = useState<CompoundSeries | null>(null)
  const [seriesLoading, setSeriesLoading] = useState(false)

  const unsubRef = useRef<(() => void) | null>(null)
  const cancelRequestedRef = useRef(false)

  const addLog = useCallback((level: string, text: string): void => {
    setLogs((prev) => [...prev.slice(-300), { level, text }])
  }, [])

  const finish = useCallback(async (id: string): Promise<void> => {
    try {
      const snap = await getInterpolation(id)
      setSnapshot(snap)
      setSelected((prev) => prev ?? snap.compounds[0]?.compound_id ?? null)
    } catch (err: unknown) {
      setErrorMessage(err instanceof Error ? err.message : String(err))
    }
    store(null)
    setScreen('results')
  }, [])

  const handleEvent = useCallback((event: InterpolationEvent, id: string): void => {
    switch (event.type) {
      case 'stage':
        setCurrentStage(event.stage)
        setStageMessage(event.message)
        addLog('info', `[Stage ${event.stage}/${event.total}] ${event.message}`)
        break
      case 'compound':
        setTotalCompounds(event.total)
        setCompounds((prev) => [
          ...prev,
          {
            compoundId: event.compound_id,
            index: event.index,
            originalPoints: event.original_points,
            interpolatedPoints: event.interpolated_points,
            bestMethod: event.best_method
          }
        ])
        addLog(
          'info',
          `  ${event.compound_id}: ${event.original_points} → ${event.interpolated_points} pts` +
            (event.best_method ? ` (best: ${event.best_method})` : '')
        )
        break
      case 'log':
        addLog(event.level, event.message)
        break
      case 'result':
        void finish(id)
        break
      case 'error':
        setErrorMessage(event.message)
        store(null)
        setScreen('results')
        break
    }
  }, [addLog, finish])

  const subscribe = useCallback((id: string): void => {
    unsubRef.current?.()
    unsubRef.current = subscribeToInterpolation(
      id,
      (event) => handleEvent(event, id),
      setStreamStatus
    )
  }, [handleEvent])

  // Pick up a run left behind by a previous page load.
  useEffect(() => {
    const stored = readStored()
    if (!stored) return

    let abandoned = false
    setResuming(true)
    getInterpolation(stored)
      .then((snap) => {
        if (abandoned) return
        setJobId(snap.job_id)
        setFilename(snap.filename)
        setStepSize(snap.step_size)
        setScreen('processing')
        subscribe(snap.job_id)
      })
      .catch(() => store(null))
      .finally(() => {
        if (!abandoned) setResuming(false)
      })

    return () => {
      abandoned = true
    }
  }, [subscribe])

  useEffect(() => {
    return () => {
      unsubRef.current?.()
      unsubRef.current = null
    }
  }, [])

  // Load the selected compound's full series for the charts.
  useEffect(() => {
    if (!jobId || !selected || screen !== 'results') return
    let abandoned = false
    setSeriesLoading(true)
    getCompoundSeries(jobId, selected)
      .then((data) => {
        if (!abandoned) setSeries(data)
      })
      .catch(() => {
        if (!abandoned) setSeries(null)
      })
      .finally(() => {
        if (!abandoned) setSeriesLoading(false)
      })
    return () => {
      abandoned = true
    }
  }, [jobId, selected, screen])

  const handleStart = async (): Promise<void> => {
    if (!file) return

    cancelRequestedRef.current = false
    setScreen('processing')
    setCurrentStage(0)
    setStageMessage('')
    setCompounds([])
    setTotalCompounds(0)
    setLogs([])
    setErrorMessage(null)
    setSnapshot(null)
    setSeries(null)
    setSelected(null)
    setFilename(file.name)

    try {
      const created = await createInterpolation(file, stepSize)
      if (cancelRequestedRef.current) {
        await cancelInterpolation(created.job_id).catch(() => undefined)
        setScreen('home')
        return
      }
      setJobId(created.job_id)
      store(created.job_id)
      subscribe(created.job_id)
    } catch (err: unknown) {
      setErrorMessage(err instanceof Error ? err.message : String(err))
      setScreen('results')
    }
  }

  const handleCancel = async (): Promise<void> => {
    cancelRequestedRef.current = true
    if (jobId) {
      try {
        await cancelInterpolation(jobId)
      } catch {
        // fall through — still go home
      }
    }
    unsubRef.current?.()
    unsubRef.current = null
    store(null)
    setStreamStatus('closed')
    setScreen('home')
  }

  const handleReset = (): void => {
    unsubRef.current?.()
    unsubRef.current = null
    store(null)
    setStreamStatus('closed')
    setScreen('home')
    setFile(null)
    setFilename('')
    setJobId(null)
    setSnapshot(null)
    setSeries(null)
    setSelected(null)
    setCompounds([])
    setLogs([])
    setErrorMessage(null)
  }

  if (screen === 'processing') {
    return (
      <InterpolationProcessing
        filename={filename}
        currentStage={currentStage}
        stageMessage={stageMessage}
        compounds={compounds}
        totalCompounds={totalCompounds}
        logs={logs}
        streamStatus={streamStatus}
        onCancel={handleCancel}
      />
    )
  }

  if (screen === 'results' && snapshot) {
    return (
      <InterpolationResults
        snapshot={snapshot}
        series={series}
        seriesLoading={seriesLoading}
        selected={selected}
        errorMessage={errorMessage}
        onSelect={setSelected}
        onReset={handleReset}
      />
    )
  }

  if (screen === 'results') {
    // Failed before any results existed.
    return (
      <div className="h-full flex items-center justify-center px-8">
        <div className="max-w-lg w-full space-y-4">
          <div className="flex items-start gap-3 px-4 py-3 rounded-xl bg-red-900/30 border border-red-700/50">
            <div>
              <p className="text-sm font-semibold text-red-300">Interpolation failed</p>
              {errorMessage && (
                <p className="text-xs text-red-500 mt-0.5 whitespace-pre-wrap">{errorMessage}</p>
              )}
            </div>
          </div>
          <button
            onClick={handleReset}
            className="w-full px-4 py-3 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 text-sm font-semibold transition-colors"
          >
            Try another file
          </button>
        </div>
      </div>
    )
  }

  return (
    <InterpolationHome
      file={file}
      stepSize={stepSize}
      resuming={resuming}
      onFileSelected={setFile}
      onStepSize={setStepSize}
      onStart={handleStart}
    />
  )
}

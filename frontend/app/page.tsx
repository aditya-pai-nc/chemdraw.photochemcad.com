'use client'

import { useState, useEffect, useRef, useCallback, type JSX } from 'react'
import type { AppScreen, CompoundRow, PipelineEvent } from '@/lib/types'
import { HomeScreen } from '@/components/HomeScreen'
import { ProcessingScreen } from '@/components/ProcessingScreen'
import { ResultsScreen } from '@/components/ResultsScreen'
import {
  cancelJob,
  checkChemDraw,
  createJob,
  getJob,
  getQueue,
  subscribeToJob,
  type QueueSnapshot,
  type StreamStatus
} from '@/lib/api'

// Remembering the job id lets a refresh (or an accidental tab close) pick the
// same job back up instead of stranding it, which matters once this is on the
// web rather than running on the user's own desktop.
const ACTIVE_JOB_KEY = 'chemdraw:activeJob'

function readStoredJobId(): string | null {
  try {
    return window.localStorage.getItem(ACTIVE_JOB_KEY)
  } catch {
    return null
  }
}

function storeJobId(id: string | null): void {
  try {
    if (id) window.localStorage.setItem(ACTIVE_JOB_KEY, id)
    else window.localStorage.removeItem(ACTIVE_JOB_KEY)
  } catch {
    // private browsing / storage disabled — resuming just won't be available
  }
}

export default function Page(): JSX.Element {
  const [screen, setScreen] = useState<AppScreen>('home')
  const [file, setFile] = useState<File | null>(null)
  const [filename, setFilename] = useState('')
  const [jobId, setJobId] = useState<string | null>(null)
  const [chemDrawAvailable, setChemDrawAvailable] = useState<boolean | null>(null)
  const [chemDrawReason, setChemDrawReason] = useState<string | null>(null)

  const [currentStage, setCurrentStage] = useState(0)
  const [stageMessage, setStageMessage] = useState('')
  const [compounds, setCompounds] = useState<CompoundRow[]>([])
  const [totalCompounds, setTotalCompounds] = useState(0)
  const [logs, setLogs] = useState<{ level: string; text: string }[]>([])
  const [errorMessage, setErrorMessage] = useState<string | null>(null)

  const [queuePosition, setQueuePosition] = useState(0)
  const [queueDepth, setQueueDepth] = useState(0)
  const [streamStatus, setStreamStatus] = useState<StreamStatus>('closed')
  const [startedAt, setStartedAt] = useState<number | null>(null)
  const [elapsedMs, setElapsedMs] = useState(0)
  const [serverQueue, setServerQueue] = useState<QueueSnapshot | null>(null)
  const [resuming, setResuming] = useState(false)

  const unsubRef = useRef<(() => void) | null>(null)
  // Guards the window between "Start" and the upload returning a job id: a
  // cancel in that gap would otherwise leave the job running on the server.
  const cancelRequestedRef = useRef(false)

  const addLog = useCallback((level: string, text: string): void => {
    setLogs((prev) => [...prev.slice(-300), { level, text }])
  }, [])

  const handleEvent = useCallback((event: PipelineEvent): void => {
    switch (event.type) {
      case 'queue':
        setQueuePosition(event.position)
        setQueueDepth(event.depth)
        addLog('info', event.message)
        break
      case 'stage':
        // Stage events only arrive once this job owns ChemDraw.
        setQueuePosition(0)
        setCurrentStage(event.stage)
        setStageMessage(event.message)
        addLog('info', `[Stage ${event.stage}/${event.total}] ${event.message}`)
        break
      case 'compound':
        setTotalCompounds(event.total)
        setCompounds((prev) => [...prev, { name: event.name, match: event.match, index: event.index }])
        addLog('info', `  ${event.match}  ${event.name}`)
        break
      case 'log':
        addLog(event.level, event.message)
        break
      case 'result':
        storeJobId(null)
        setScreen('results')
        break
      case 'error':
        setErrorMessage(event.message)
        storeJobId(null)
        setScreen('results')
        break
    }
  }, [addLog])

  const subscribe = useCallback((id: string): void => {
    unsubRef.current?.()
    unsubRef.current = subscribeToJob(id, handleEvent, setStreamStatus)
  }, [handleEvent])

  useEffect(() => {
    checkChemDraw()
      .then((r) => {
        setChemDrawAvailable(r.available)
        setChemDrawReason(r.available ? null : (r.reason ?? null))
      })
      .catch((err: unknown) => {
        setChemDrawAvailable(false)
        setChemDrawReason(err instanceof Error ? err.message : String(err))
      })
  }, [])

  // Pick up a job left running by a previous page load. The stream replays from
  // the start, so the compound table and log rebuild themselves.
  useEffect(() => {
    const stored = readStoredJobId()
    if (!stored) return

    let abandoned = false
    setResuming(true)
    getJob(stored)
      .then((snap) => {
        if (abandoned) return
        setJobId(snap.job_id)
        setFilename(snap.filename)
        setStartedAt(snap.created_at * 1000)
        setQueuePosition(Math.max(0, snap.queue_position))
        setQueueDepth(snap.queue_depth)
        setScreen('processing')
        subscribe(snap.job_id)
      })
      .catch(() => storeJobId(null)) // job is gone (server restarted, or expired)
      .finally(() => {
        if (!abandoned) setResuming(false)
      })

    return () => {
      abandoned = true
    }
  }, [subscribe])

  // Close the stream when the page goes away.
  useEffect(() => {
    return () => {
      unsubRef.current?.()
      unsubRef.current = null
    }
  }, [])

  // Elapsed clock, anchored to the server's own timestamp so it survives reloads.
  useEffect(() => {
    if (screen !== 'processing' || startedAt === null) return
    const tick = (): void => setElapsedMs(Date.now() - startedAt)
    tick()
    const timer = setInterval(tick, 1000)
    return () => clearInterval(timer)
  }, [screen, startedAt])

  // While idle, show how busy ChemDraw is so the wait is no surprise.
  useEffect(() => {
    if (screen !== 'home') return
    let alive = true
    const poll = (): void => {
      getQueue()
        .then((q) => {
          if (alive) setServerQueue(q)
        })
        .catch(() => {
          if (alive) setServerQueue(null)
        })
    }
    poll()
    const timer = setInterval(poll, 5000)
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [screen])

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
    setJobId(null)
    setQueuePosition(0)
    setQueueDepth(0)
    setFilename(file.name)
    setStartedAt(Date.now())
    setElapsedMs(0)

    try {
      const created = await createJob(file)

      // Cancelled while the upload was in flight — stop it and go back.
      if (cancelRequestedRef.current) {
        await cancelJob(created.job_id).catch(() => undefined)
        setScreen('home')
        return
      }

      setJobId(created.job_id)
      storeJobId(created.job_id)
      setQueuePosition(created.queue_position)
      setQueueDepth(created.queue_depth)
      subscribe(created.job_id)
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err)
      setErrorMessage(msg)
      setScreen('results')
    }
  }

  const handleCancel = async (): Promise<void> => {
    cancelRequestedRef.current = true
    if (jobId) {
      try {
        await cancelJob(jobId)
      } catch {
        // still return home even if the cancel request fails
      }
    }
    unsubRef.current?.()
    unsubRef.current = null
    storeJobId(null)
    setStreamStatus('closed')
    setScreen('home')
  }

  const handleReset = (): void => {
    unsubRef.current?.()
    unsubRef.current = null
    storeJobId(null)
    setStreamStatus('closed')
    setScreen('home')
    setFile(null)
    setFilename('')
    setJobId(null)
    setCompounds([])
    setLogs([])
    setErrorMessage(null)
    setQueuePosition(0)
    setQueueDepth(0)
    setStartedAt(null)
    setElapsedMs(0)
  }

  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="flex items-center gap-3 px-5 py-2.5 border-b border-slate-800 shrink-0">
        <span className="text-xs font-semibold text-slate-500 uppercase tracking-widest">
          ChemDraw Processor
        </span>
        {screen === 'home' && serverQueue !== null && serverQueue.depth > 0 && (
          <span className="text-xs text-amber-400">
            {serverQueue.depth} {serverQueue.depth === 1 ? 'job' : 'jobs'} in the queue
          </span>
        )}
        {chemDrawAvailable === true && (
          <span className="ml-auto text-xs text-emerald-400 flex items-center gap-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 inline-block" />
            ChemDraw detected
          </span>
        )}
        {chemDrawAvailable === false && (
          <span className="ml-auto text-xs text-red-400 flex items-center gap-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-red-400 inline-block" />
            ChemDraw not found
          </span>
        )}
      </div>

      <div className="flex-1 min-h-0">
        {screen === 'home' && (
          <HomeScreen
            file={file}
            chemDrawAvailable={chemDrawAvailable}
            chemDrawReason={chemDrawReason}
            queueDepth={serverQueue?.depth ?? 0}
            resuming={resuming}
            onFileSelected={setFile}
            onStart={handleStart}
          />
        )}
        {screen === 'processing' && (
          <ProcessingScreen
            filename={filename}
            currentStage={currentStage}
            stageMessage={stageMessage}
            compounds={compounds}
            totalCompounds={totalCompounds}
            logs={logs}
            queuePosition={queuePosition}
            queueDepth={queueDepth}
            streamStatus={streamStatus}
            elapsedMs={elapsedMs}
            onCancel={handleCancel}
          />
        )}
        {screen === 'results' && (
          <ResultsScreen
            jobId={jobId}
            compounds={compounds}
            errorMessage={errorMessage}
            onReset={handleReset}
          />
        )}
      </div>
    </div>
  )
}

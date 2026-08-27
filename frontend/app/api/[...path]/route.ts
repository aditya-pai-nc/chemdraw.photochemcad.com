/**
 * Server-side proxy: the browser only ever talks to Next, and Next talks to
 * FastAPI. That keeps the Python service bound to localhost — never exposed to
 * the internet — and leaves one origin, so there is no CORS to configure and
 * one place to add auth or rate limiting later.
 *
 * Everything is streamed rather than buffered, so SSE progress arrives live and
 * large .xlsx/.zip downloads never sit in Node's memory.
 */
import { NextRequest } from 'next/server'

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

const BACKEND_URL = process.env.CHEMDRAW_API_URL ?? 'http://127.0.0.1:8000'

// Request headers worth forwarding. `last-event-id` matters most: it is how a
// reconnecting EventSource resumes instead of replaying the whole job.
const FORWARD_REQUEST_HEADERS = ['content-type', 'accept', 'last-event-id', 'range']

// Response headers worth returning. content-length is deliberately dropped —
// the body is re-streamed, so letting the runtime chunk it avoids any chance of
// a mismatch truncating a download.
const FORWARD_RESPONSE_HEADERS = ['content-type', 'content-disposition', 'cache-control']

async function proxy(request: NextRequest, path: string[]): Promise<Response> {
  const target = new URL(`/api/${path.join('/')}`, BACKEND_URL)
  target.search = request.nextUrl.search

  const headers = new Headers()
  for (const name of FORWARD_REQUEST_HEADERS) {
    const value = request.headers.get(name)
    if (value) headers.set(name, value)
  }

  const init: RequestInit & { duplex?: 'half' } = {
    method: request.method,
    headers,
    cache: 'no-store',
    redirect: 'manual'
  }

  if (request.method !== 'GET' && request.method !== 'HEAD') {
    // Streamed, so a 50 MB .cdx upload never sits in Node's heap.
    init.body = request.body
    // Required by undici whenever the body is a stream.
    init.duplex = 'half'
  }

  let upstream: Response
  try {
    upstream = await fetch(target, init)
  } catch (error: unknown) {
    const detail = error instanceof Error ? error.message : String(error)
    return Response.json(
      { detail: `Cannot reach the processing backend at ${BACKEND_URL}. ${detail}` },
      { status: 502 }
    )
  }

  const responseHeaders = new Headers()
  for (const name of FORWARD_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name)
    if (value) responseHeaders.set(name, value)
  }

  // Event streams must not be buffered anywhere between here and the browser.
  if (responseHeaders.get('content-type')?.includes('text/event-stream')) {
    responseHeaders.set('cache-control', 'no-cache, no-transform')
    responseHeaders.set('connection', 'keep-alive')
    responseHeaders.set('x-accel-buffering', 'no')
  }

  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders
  })
}

type Context = { params: Promise<{ path: string[] }> }

export async function GET(request: NextRequest, context: Context): Promise<Response> {
  return proxy(request, (await context.params).path)
}

export async function POST(request: NextRequest, context: Context): Promise<Response> {
  return proxy(request, (await context.params).path)
}

export async function DELETE(request: NextRequest, context: Context): Promise<Response> {
  return proxy(request, (await context.params).path)
}

export async function PATCH(request: NextRequest, context: Context): Promise<Response> {
  return proxy(request, (await context.params).path)
}

export async function PUT(request: NextRequest, context: Context): Promise<Response> {
  return proxy(request, (await context.params).path)
}

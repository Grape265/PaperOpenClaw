/**
 * HTTP client for the PaperOpenClaw API.
 *
 * Responsibilities kept here so no component ever builds a URL or unwraps an
 * envelope by hand:
 *   - the `/api/v1` prefix and query-string encoding;
 *   - turning the `{ok, data, error}` envelope into a resolved value or a
 *     thrown `ApiError` carrying the contract's error `code`;
 *   - streaming SSE, including abort handling.
 */

import type { ApiErrorBody, Envelope } from './types'

// 网页模式（Python 后端托管前端）用相对路径；桌面模式（Tauri）用绝对地址指向本地后端。
export const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined) || '/api/v1'

export class ApiError extends Error {
  readonly code: string
  readonly status: number
  readonly details?: Record<string, unknown>

  constructor(body: ApiErrorBody, status: number) {
    super(body.message || body.code)
    this.name = 'ApiError'
    this.code = body.code
    this.status = status
    this.details = body.details
  }
}

type QueryValue = string | number | boolean | null | undefined | Array<string | number>

function buildUrl(path: string, params?: Record<string, QueryValue>): string {
  const url = path.startsWith('http') ? path : `${API_BASE}${path}`
  if (!params) return url
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === '') continue
    if (Array.isArray(value)) {
      if (value.length === 0) continue
      search.append(key, value.join(','))
    } else {
      search.append(key, String(value))
    }
  }
  const query = search.toString()
  return query ? `${url}?${query}` : url
}

interface RequestOptions {
  params?: Record<string, QueryValue>
  body?: unknown
  files?: File[]
  signal?: AbortSignal
  formData?: FormData
}

async function request<T>(method: string, path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = {}
  let payload: BodyInit | undefined

  if (options.formData) {
    payload = options.formData
  } else if (options.files && options.files.length > 0) {
    const form = new FormData()
    for (const file of options.files) form.append('files', file, file.name)
    payload = form
  } else if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json'
    payload = JSON.stringify(options.body)
  }

  let response: Response
  try {
    response = await fetch(buildUrl(path, options.params), {
      method,
      headers,
      body: payload,
      signal: options.signal,
    })
  } catch (error) {
    if ((error as Error).name === 'AbortError') throw error
    throw new ApiError(
      { code: 'NETWORK_ERROR', message: `无法连接后端服务：${(error as Error).message}` },
      0,
    )
  }

  const text = await response.text()
  let envelope: Envelope<T> | null = null
  if (text) {
    try {
      envelope = JSON.parse(text) as Envelope<T>
    } catch {
      // A non-JSON body means something upstream failed (proxy, crash).
      throw new ApiError(
        { code: 'BAD_RESPONSE', message: text.slice(0, 300) || '服务端返回了非 JSON 响应' },
        response.status,
      )
    }
  }

  if (!envelope) {
    if (response.ok) return undefined as T
    throw new ApiError({ code: 'EMPTY_RESPONSE', message: `HTTP ${response.status}` }, response.status)
  }
  if (!envelope.ok) {
    throw new ApiError(envelope.error ?? { code: 'UNKNOWN', message: '未知错误' }, response.status)
  }
  return envelope.data
}

export const http = {
  get: <T>(path: string, params?: Record<string, QueryValue>, signal?: AbortSignal) =>
    request<T>('GET', path, { params, signal }),
  post: <T>(path: string, body?: unknown, params?: Record<string, QueryValue>) =>
    request<T>('POST', path, { body, params }),
  put: <T>(path: string, body?: unknown) => request<T>('PUT', path, { body }),
  patch: <T>(path: string, body?: unknown) => request<T>('PATCH', path, { body }),
  del: <T>(path: string, params?: Record<string, QueryValue>) =>
    request<T>('DELETE', path, { params }),
  upload: <T>(path: string, files: File[], params?: Record<string, QueryValue>) =>
    request<T>('POST', path, { files, params }),
  uploadOne: <T>(path: string, file: File, params?: Record<string, QueryValue>) => {
    const form = new FormData()
    form.append('file', file, file.name)
    return request<T>('POST', path, { formData: form, params })
  },
}

export interface SseHandlers {
  onEvent: (event: string, data: Record<string, any>) => void
  onError?: (error: Error) => void
  onClose?: () => void
}

/**
 * Open an SSE stream and dispatch decoded events.
 *
 * `fetch` is used rather than `EventSource` because the QA and job streams are
 * POST-initiated and need request bodies and headers, which `EventSource`
 * cannot send.
 */
export async function streamSse(
  path: string,
  options: { method?: string; body?: unknown; signal?: AbortSignal; params?: Record<string, QueryValue> },
  handlers: SseHandlers,
): Promise<void> {
  const { method = 'POST', body, signal, params } = options
  const headers: Record<string, string> = { Accept: 'text/event-stream' }
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  let response: Response
  try {
    response = await fetch(buildUrl(path, params), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    })
  } catch (error) {
    if ((error as Error).name !== 'AbortError') handlers.onError?.(error as Error)
    handlers.onClose?.()
    return
  }

  if (!response.ok) {
    let message = `HTTP ${response.status}`
    try {
      const parsed = JSON.parse(await response.text())
      message = parsed?.error?.message ?? message
    } catch {
      /* keep the status-based message */
    }
    handlers.onError?.(new ApiError({ code: 'STREAM_FAILED', message }, response.status))
    handlers.onClose?.()
    return
  }
  if (!response.body) {
    handlers.onError?.(new ApiError({ code: 'STREAM_EMPTY', message: '响应没有数据流' }, 200))
    handlers.onClose?.()
    return
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      let boundary = buffer.indexOf('\n\n')
      while (boundary !== -1) {
        const raw = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        const parsed = parseSseBlock(raw)
        if (parsed) handlers.onEvent(parsed.event, parsed.data)
        boundary = buffer.indexOf('\n\n')
      }
    }
  } catch (error) {
    if ((error as Error).name !== 'AbortError') handlers.onError?.(error as Error)
  } finally {
    try {
      reader.releaseLock()
    } catch {
      /* already released */
    }
    handlers.onClose?.()
  }
}

function parseSseBlock(raw: string): { event: string; data: Record<string, any> } | null {
  let event = 'message'
  const dataLines: string[] = []
  for (const line of raw.split('\n')) {
    if (line.startsWith(':')) continue // heartbeat comment
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
  }
  if (dataLines.length === 0) return null
  const joined = dataLines.join('\n')
  try {
    return { event, data: JSON.parse(joined) }
  } catch {
    return { event, data: { text: joined } }
  }
}

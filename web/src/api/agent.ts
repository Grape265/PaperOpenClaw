/** Agent endpoints: catalogues, QA (including SSE), brainstorm, sessions. */

import { http, streamSse } from './client'
import type { AgentProfile, Paged, QaResult, SessionDetail, SessionSummary, ToolInfo } from './types'

export interface BrainstormPayload {
  topic: string
  hashes?: string[]
  agents?: {
    id?: string
    profile_id?: string
    name?: string
    persona?: string
    provider?: string
    model?: string
    tools?: string[]
    hashes?: string[]
  }[]
  rounds?: number
  moderator?: { name?: string; provider?: string; model?: string }
}

export interface QaStreamHandlers {
  onStatus?: (data: Record<string, any>) => void
  onCitations?: (data: Record<string, any>) => void
  onDelta?: (text: string) => void
  onDone?: (data: Record<string, any>) => void
  onError?: (data: Record<string, any>) => void
}

export const agentApi = {
  profiles: () => http.get<{ items: AgentProfile[] }>('/agent/profiles'),

  tools: () => http.get<{ items: ToolInfo[] }>('/agent/tools'),

  qa: (payload: { question: string; hashes?: string[]; top_k?: Record<string, number>; session_id?: string }) =>
    http.post<QaResult>('/agent/qa', payload),

  /** Stream a QA answer. Returns a close function. */
  qaStream(
    payload: { question: string; hashes?: string[]; top_k?: Record<string, number>; session_id?: string },
    handlers: QaStreamHandlers,
  ): () => void {
    const controller = new AbortController()
    void streamSse(
      '/agent/qa/stream',
      { method: 'POST', body: payload, signal: controller.signal },
      {
        onEvent: (event, data) => {
          if (event === 'status') handlers.onStatus?.(data)
          else if (event === 'citations') handlers.onCitations?.(data)
          else if (event === 'delta') handlers.onDelta?.(String(data.text ?? ''))
          else if (event === 'done') handlers.onDone?.(data)
          else if (event === 'error') handlers.onError?.(data)
        },
        onError: (error) => handlers.onError?.({ code: 'STREAM_FAILED', message: error.message }),
        onClose: () => undefined,
      },
    )
    return () => controller.abort()
  },

  run: (payload: { task: string; profile?: string; hashes?: string[]; tools?: string[]; session_id?: string }) =>
    http.post<{
      session_id: string
      agent_id: string
      answer: string
      steps: number
      tool_calls: { step: number; name: string; arguments: Record<string, any>; ok: boolean; error: string }[]
      usage: Record<string, number>
      elapsed_ms: number
      error: string
    }>('/agent/run', payload),

  brainstorm: (payload: BrainstormPayload) =>
    http.post<{ session_id: string; status: string }>('/agent/brainstorm', payload),

  continueBrainstorm: (sessionId: string, message?: string) =>
    http.post<{ session_id: string; status: string }>(`/agent/sessions/${sessionId}/continue`, {
      message: message ?? null,
    }),

  sessions: (params: { kind?: string; page?: number; page_size?: number } = {}) =>
    http.get<Paged<SessionSummary>>('/agent/sessions', params),

  session: (sessionId: string) => http.get<SessionDetail>(`/agent/sessions/${sessionId}`),

  sessionBlackboard: (sessionId: string) =>
    http.get<{ items: Record<string, any>[] }>(`/agent/sessions/${sessionId}/blackboard`),

  postMessage: (sessionId: string, content: string) =>
    http.post<{ message_id: number; session_id: string }>(
      `/agent/sessions/${sessionId}/message`,
      { content },
    ),

  deleteSession: (sessionId: string) =>
    http.del<{ session_id: string; deleted: boolean }>(`/agent/sessions/${sessionId}`),

  /** Subscribe to a brainstorm's live events. Returns a close function. */
  watchSession(
    sessionId: string,
    handlers: {
      onRound?: (data: Record<string, any>) => void
      onPlan?: (data: Record<string, any>) => void
      onMessage?: (data: Record<string, any>) => void
      onBlackboard?: (data: Record<string, any>) => void
      onAwaitingUser?: (data: Record<string, any>) => void
      onSynthesis?: (data: Record<string, any>) => void
      onDone?: (data: Record<string, any>) => void
      onError?: (data: Record<string, any>) => void
    },
  ): () => void {
    const controller = new AbortController()
    void streamSse(
      `/agent/sessions/${sessionId}/events`,
      { method: 'GET', signal: controller.signal },
      {
        onEvent: (event, data) => {
          if (event === 'round') handlers.onRound?.(data)
          else if (event === 'plan') handlers.onPlan?.(data)
          else if (event === 'message') handlers.onMessage?.(data)
          else if (event === 'blackboard') handlers.onBlackboard?.(data)
          else if (event === 'awaiting_user') handlers.onAwaitingUser?.(data)
          else if (event === 'synthesis') handlers.onSynthesis?.(data)
          else if (event === 'done') handlers.onDone?.(data)
          else if (event === 'error') handlers.onError?.(data)
        },
        onError: (error) => handlers.onError?.({ code: 'STREAM_FAILED', message: error.message }),
        onClose: () => undefined,
      },
    )
    return () => controller.abort()
  },
}

/** Ingest and job endpoints, including the SSE progress stream. */

import { API_BASE, http, streamSse } from './client'
import type { IngestResult, Job, Paged } from './types'

export const ingestApi = {
  upload: (files: File[], use_llm = true) =>
    http.upload<IngestResult>('/ingest/upload', files, { use_llm }),

  jobs: (params: { status?: string; hash?: string; page?: number; page_size?: number } = {}) =>
    http.get<Paged<Job>>('/jobs', params),

  job: (jobId: string) => http.get<Job>(`/jobs/${jobId}`),

  deleteJob: (jobId: string) => http.del<{ job_id: string; deleted: boolean }>(`/jobs/${jobId}`),

  cancel: (jobId: string) =>
    http.post<{ job_id: string; status: string; cancelled: boolean }>(`/jobs/${jobId}/cancel`),

  exportUrl: () => `${API_BASE}/jobs/export`,

  /** Subscribe to a job's progress. Returns a function that closes the stream. */
  watchJob(
    jobId: string,
    handlers: {
      onStage?: (data: Record<string, any>) => void
      onProgress?: (data: Record<string, any>) => void
      onLog?: (data: Record<string, any>) => void
      onDone?: (data: Record<string, any>) => void
      onError?: (data: Record<string, any>) => void
    },
  ): () => void {
    const controller = new AbortController()
    void streamSse(
      `/jobs/${jobId}/events`,
      { method: 'GET', signal: controller.signal },
      {
        onEvent: (event, data) => {
          if (event === 'stage') handlers.onStage?.(data)
          else if (event === 'progress') handlers.onProgress?.(data)
          else if (event === 'log') handlers.onLog?.(data)
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

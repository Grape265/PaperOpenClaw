/** Paper endpoints: listing, detail, PDF/assets/OKF access, deletion. */

import { API_BASE, http } from './client'
import type {
  Asset,
  Job,
  OkfDocument,
  OkfVerifyReport,
  Paged,
  PaperDetail,
  PaperSummary,
} from './types'

export interface PaperQuery {
  q?: string
  hash?: string
  hashes?: string[]
  author?: string
  journal?: string
  year_from?: number | null
  year_to?: number | null
  status?: string
  sort?: string
  page?: number
  page_size?: number
}

export interface Facets {
  journals: string[]
  year_from: number | null
  year_to: number | null
  statuses: { value: string; count: number }[]
  total: number
}

export const papersApi = {
  list: (query: PaperQuery = {}) =>
    http.get<Paged<PaperSummary>>('/papers', {
      q: query.q,
      hash: query.hash,
      hashes: query.hashes,
      author: query.author,
      journal: query.journal,
      year_from: query.year_from ?? undefined,
      year_to: query.year_to ?? undefined,
      status: query.status,
      sort: query.sort,
      page: query.page,
      page_size: query.page_size,
    }),

  facets: () => http.get<Facets>('/papers/facets'),

  get: (hash: string) => http.get<PaperDetail>(`/papers/${hash}`),

  status: (hash: string) =>
    http.get<{ hash: string; status: string; error: string; counts: Record<string, number> }>(
      `/papers/${hash}/status`,
    ),

  remove: (hash: string) => http.del<{ hash: string; deleted: boolean; files: boolean }>(`/papers/${hash}`),

  reparse: (hash: string, fromStage = 'vision', force = true) =>
    http.post<{ job_id: string; hash: string }>(`/papers/${hash}/reparse`, {
      from_stage: fromStage,
      force,
    }),

  assets: (hash: string, kind = '') =>
    http.get<{ items: Asset[] }>(`/papers/${hash}/assets`, { kind }),

  okfTree: (hash: string) =>
    http.get<{ okf_version: string; root: string; exists: boolean; tree: any[] }>(
      `/papers/${hash}/okf`,
    ),

  okfConcept: (hash: string, conceptId: string) =>
    http.get<OkfDocument>(`/papers/${hash}/okf/${conceptId}`),

  okfVerify: (hash: string) => http.get<OkfVerifyReport>(`/papers/${hash}/okf/verify`),

  meta: (hash: string) => http.get<Record<string, any>>(`/papers/${hash}/meta.json`),

  recentJobs: (hash: string) => http.get<Paged<Job>>('/jobs', { hash, page_size: 5 }),

  // Direct URLs - used by <img>, <iframe> and download links, so they are built
  // rather than fetched.
  pdfUrl: (hash: string) => `${API_BASE}/papers/${hash}/pdf`,
  assetUrl: (hash: string, label: string) => `${API_BASE}/papers/${hash}/assets/${label}`,
  exportUrl: (hash: string) => `${API_BASE}/papers/${hash}/export`,
}

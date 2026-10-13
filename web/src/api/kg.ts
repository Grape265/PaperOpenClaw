/** Knowledge graph endpoints: spine, fishbone, subgraph, search, export. */

import { API_BASE, http } from './client'
import type { FishboneSection, KgGraph, KgNode, SpineDefinition } from './types'

export interface FishboneResponse {
  hash: string
  title: string
  spine: FishboneSection[]
  counts: { sections: number; nodes: number; assets: number }
}

export const kgApi = {
  sections: () => http.get<{ items: SpineDefinition[] }>('/kg/sections'),

  fishbone: (hash: string) => http.get<FishboneResponse>(`/kg/fishbone/${hash}`),

  graph: (params: { hashes?: string[]; sections?: string[]; depth?: number; limit?: number } = {}) =>
    http.get<KgGraph>('/kg/graph', {
      hashes: params.hashes,
      sections: params.sections,
      depth: params.depth,
      limit: params.limit,
    }),

  search: (params: { q: string; hashes?: string[]; sections?: string[]; limit?: number }) =>
    http.get<{ items: KgNode[]; total: number }>('/kg/search', params),

  path: (source: string, target: string) =>
    http.get<{
      source: string
      target: string
      found: boolean
      path: { key: string; label: string; kind: string; hash: string }[]
    }>('/kg/path', { source, target }),

  stats: (hashes?: string[]) => http.get<Record<string, any>>('/kg/stats', { hashes }),

  exportUrl: (params: { hashes?: string[]; format?: string } = {}) => {
    const search = new URLSearchParams()
    if (params.hashes?.length) search.append('hashes', params.hashes.join(','))
    search.append('format', params.format ?? 'json')
    return `${API_BASE}/kg/export?${search.toString()}`
  },
}

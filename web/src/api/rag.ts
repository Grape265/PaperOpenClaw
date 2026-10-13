/** RAG endpoints: cascade search, pyramid inspection, structure graph. */

import { http } from './client'
import type { Evidence, Pyramid, RagOverview, RagSearchResult, RagUnit } from './types'

export interface RagSearchPayload {
  query: string
  hashes?: string[]
  layers?: string[]
  top_k?: { c?: number; b?: number; a?: number }
  min_score?: number
  cascade?: boolean
}

export interface UnitNeighbour {
  unit_id: string
  layer: string
  title: string
  section: string
  weight: number
  relation: string
}

export interface UnitDetail {
  unit: RagUnit & { section_label: string; paper_title: string }
  parents: UnitNeighbour[]
  children: UnitNeighbour[]
  cross_links: { unit_id: string; hash: string; title: string; weight: number }[]
}

export interface RagGraph {
  nodes: {
    unit_id: string
    layer: string
    hash: string
    paper_title: string
    title: string
    section: string
    section_label: string
    chars: number
    cross_link_count: number
  }[]
  edges: { source: string; target: string; relation: string; weight: number }[]
  counts: { nodes: number; edges: number; by_layer: Record<string, number> }
}

export const ragApi = {
  search: (payload: RagSearchPayload) => http.post<RagSearchResult>('/rag/search', payload),

  pyramid: (hash: string) => http.get<Pyramid>(`/rag/pyramid/${hash}`),

  unit: (unitId: string) => http.get<UnitDetail>(`/rag/units/${unitId}`),

  graph: (params: { hashes?: string[]; layers?: string[] } = {}) =>
    http.get<RagGraph>('/rag/graph', {
      hashes: params.hashes,
      layers: params.layers,
    }),

  overview: () => http.get<RagOverview>('/rag/overview'),

  rebuild: (payload: { hashes?: string[]; layers?: string[] } = {}) =>
    http.post<{ job_id: string }>('/rag/rebuild', payload),

  /** Citation lookup helper: turn an evidence list into display rows. */
  toCitations: (evidences: Evidence[]) =>
    evidences.map((item) => ({
      unit_id: item.unit_id,
      layer: item.layer,
      hash: item.hash,
      title: item.paper_title || item.title,
      section: item.section,
      section_label: item.section_label,
      snippet: item.text.slice(0, 280),
      score: item.score,
      asset_refs: item.asset_refs,
    })),
}

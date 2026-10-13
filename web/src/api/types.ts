/**
 * Types mirroring docs/03-api-contract.md.
 *
 * Field names match the backend wire format exactly; keeping them in one file
 * means a contract change surfaces as a compile error rather than a runtime
 * surprise.
 */

export interface Envelope<T> {
  ok: boolean
  data: T
  error: ApiErrorBody | null
  meta: Record<string, unknown> & { request_id?: string }
}

export interface ApiErrorBody {
  code: string
  message: string
  details?: Record<string, unknown>
}

export interface Paged<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

/* ------------------------------------------------------------------ papers */

export interface Author {
  name: string
  affiliations: string[]
  is_corresponding?: boolean
  email?: string
}

export interface PaperCounts {
  figures: number
  tables: number
  rag_a: number
  rag_b: number
  rag_c: number
  kg_nodes: number
  okf_concepts?: number
  authors?: number
}

export interface PaperSummary {
  hash: string
  hash_short: string
  title: string
  title_original?: string
  journal: string
  venue_type?: string
  published_at: string
  year: number | null
  doi?: string
  language: string
  page_count: number
  status: string
  error?: string
  created_at: string
  updated_at?: string
  keywords: string[]
  authors: Author[]
  counts: PaperCounts
}

export interface Asset {
  id?: number
  label: string
  kind: 'figure' | 'table' | string
  ordinal: number
  page: number
  bbox: number[]
  caption: string
  caption_source?: string
  content_text?: string
  section: string
  file_path: string
  width: number
  height: number
  url?: string
}

export interface OkfConcept {
  id?: number
  concept_id: string
  type: string
  title: string
  description: string
  section: string
  file_path: string
  ordinal: number
}

export interface OkfTreeNode {
  concept_id: string
  type: string
  title: string
  description: string
  section?: string
  children: OkfTreeNode[]
}

export interface RagUnit {
  id: string
  layer: 'A' | 'B' | 'C' | string
  paper_hash: string
  ordinal: number
  title: string
  text: string
  section: string
  source_ref: string
  char_count: number
  token_count: number
  meta?: Record<string, unknown>
  parents?: string[]
  children?: string[]
}

export interface PyramidLayer {
  unit_id: string
  text: string
  title?: string
  children?: string[]
  cross_links?: { hash: string; title: string; score: number }[]
}

export interface Pyramid {
  hash: string
  C: PyramidLayer | null
  B: RagUnit[]
  A: RagUnit[]
  counts: { A: number; B: number; C: number }
}

export interface KgNode {
  key: string
  node_key?: string
  kind: 'spine' | 'content' | 'asset' | 'paper' | string
  label: string
  detail: string
  section: string
  hash: string
  ordinal: number
  meta?: Record<string, unknown>
}

export interface KgEdge {
  source: string
  target: string
  src_key?: string
  dst_key?: string
  relation: string
  weight: number
}

export interface KgAssetLink {
  key: string
  label: string
  kind: string
  path: string
  url: string
  caption: string
}

export interface FishboneNode {
  key: string
  label: string
  detail: string
  ordinal: number
  unit_id: string
  assets: KgAssetLink[]
  meta?: Record<string, unknown>
}

export interface FishboneSection {
  slug: string
  label: string
  order: number
  nodes: FishboneNode[]
}

export interface PaperDetail {
  paper: PaperSummary
  abstract: string
  doi: string
  url: string
  affiliations: {
    id: number
    name: string
    department: string
    country: string
    order_index: number
  }[]
  assets: Asset[]
  okf: { root: string; concepts: OkfConcept[] }
  pyramid: { counts: { A: number; B: number; C: number }; C: PyramidLayer | null; B: RagUnit[]; A: RagUnit[] }
  kg_summary: {
    nodes: number
    sections: string[]
    spine: { slug: string; label: string; count: number }[]
  }
  ingest: Record<string, any>
  recent_jobs: Job[]
}

export interface OkfDocument {
  concept_id: string
  file_path: string
  frontmatter: Record<string, any>
  body: string
  raw: string
  links: string[]
}

export interface OkfVerifyReport {
  conformant: boolean
  files: number
  errors: { file: string; message: string }[]
  warnings: { file: string; message: string }[]
  okf_version: string
}

/* ------------------------------------------------------------------ jobs */

export interface JobStage {
  stage: string
  started_at: string
  finished_at: string | null
  duration_ms: number | null
  message: string
}

export interface Job {
  id: string
  kind: string
  hash: string
  filename: string
  stage: string
  stage_index: number
  stage_total: number
  status: 'pending' | 'running' | 'done' | 'failed' | 'cancelled' | string
  progress: number
  message: string
  error: string
  stages: JobStage[]
  usage: Record<string, number>
  started_at: string
  finished_at: string
  created_at: string
}

export interface IngestItem {
  filename: string
  hash?: string
  hash_short?: string
  job_id?: string
  duplicated?: boolean
  title?: string
  error?: string
  source_path?: string
}

export interface IngestResult {
  items: IngestItem[]
  accepted: number
  duplicated: number
  scanned?: number
  job_ids: string[]
}

/* ------------------------------------------------------------------- RAG */

export interface Candidate {
  hash: string
  hash_short: string
  title: string
  journal: string
  year: number | null
  score: number
  unit_id: string
}

export interface Evidence {
  unit_id: string
  layer: 'A' | 'B' | 'C' | string
  hash: string
  hash_short: string
  title: string
  paper_title?: string
  journal?: string
  year?: number | null
  section: string
  section_label: string
  text: string
  score: number
  trace: string[]
  asset_refs: string[]
}

export interface RagSearchResult {
  query: string
  candidates: Candidate[]
  evidences: Evidence[]
  layers_used: string[]
  empty: boolean
  reason?: string
  threshold?: number
}

export interface RagOverview {
  units: Record<string, number>
  vectors: Record<string, number>
  cross_links: number
  papers: number
  embedding: { provider: string; model: string; dimension: number; [key: string]: any }
  sections: Record<string, string>
}

/* -------------------------------------------------------------------- KG */

export interface SpineDefinition {
  slug: string
  label: string
  order: number
  description: string
}

export interface KgGraph {
  nodes: KgNode[]
  edges: KgEdge[]
  stats?: {
    nodes: number
    edges: number
    components: number
    density: number
    top_degree: { key: string; degree: number; label: string; kind: string }[]
  }
  shared_sections?: { slug: string; label: string; papers: string[]; count: number }[]
}

/* ----------------------------------------------------------------- agent */

export interface AgentProfile {
  id: string
  name: string
  persona: string
  tools: string[]
  default: boolean
}

export interface ToolInfo {
  name: string
  description: string
  source: string
  source_name: string
  params_schema: Record<string, any>
}

export interface Citation {
  unit_id: string
  layer: string
  hash: string
  title: string
  section: string
  section_label?: string
  snippet: string
  score: number
  asset_refs: string[]
}

export interface QaResult {
  answer: string
  citations: Citation[]
  session_id: string
  layers_used: string[]
  empty: boolean
  usage?: Record<string, number>
  error?: string
}

export interface SessionSummary {
  id: string
  kind: 'qa' | 'brainstorm' | 'agent' | string
  title: string
  topic: string
  paper_hashes: string[]
  status: string
  rounds: number
  created_at: string
  updated_at: string
  message_count?: number
  meta?: Record<string, any>
}

export interface SessionAgent {
  id: string
  session_id: string
  name: string
  persona: string
  profile_id: string
  provider: string
  model: string
  tools: string[]
  paper_hashes: string[]
  workspace: string
  status: string
  created_at: string
}

export interface SessionMessage {
  id: number
  session_id: string
  agent_id: string
  round: number
  role: string
  content: string
  citations: Citation[]
  usage: Record<string, number>
  created_at: string
}

export interface BlackboardEntry {
  id: number
  session_id: string
  round: number
  author_agent: string
  entry_type: string
  content: string
  citations: Citation[]
  created_at: string
}

export interface SessionDetail {
  session: SessionSummary
  agents: SessionAgent[]
  messages: SessionMessage[]
  by_agent: Record<string, SessionMessage[]>
  blackboard: BlackboardEntry[]
  synthesis: Record<string, any> | null
}

/* ---------------------------------------------------------------- skills */

export interface Skill {
  id: string
  name: string
  description: string
  version: string
  author: string
  path: string
  enabled: boolean
  tools: string[]
  installed_at: string
  updated_at: string
  source: string
}

export interface SkillDetail {
  skill: Skill
  instructions: string
  frontmatter: Record<string, any>
  tool_declarations: Record<string, any>[]
  files: string[]
  warnings: string[]
}

/* -------------------------------------------------------------- settings */

export interface ProviderConfig {
  id: string
  name: string
  base_url: string
  api_key: string
  api_key_set?: boolean
  models: string[]
  vision: boolean
  timeout: number
  max_retries: number
  enabled: boolean
}

export interface Settings {
  version: number
  data_dir: string
  providers: ProviderConfig[]
  routing: Record<string, { provider: string; model: string; temperature: number; max_tokens: number }>
  embedding: {
    provider: string
    model: string
    model_path: string
    dimension: number
    batch_size: number
    base_url: string
    api_key: string
    api_key_set?: boolean
    remote_model: string
  }
  ingest: Record<string, any>
  retrieval: Record<string, any>
  agent: Record<string, any>
  server: Record<string, any>
}

export interface ProviderPreset {
  id: string
  name: string
  base_url: string
  models: string[]
  vision: boolean
  notes: string
}

export interface HealthReport {
  status: string
  version: string
  data_dir: string
  providers: Record<string, { configured: boolean; provider: string; model: string; vision?: boolean }>
  embedding: Record<string, any>
  vectors: Record<string, any>
  counts: Record<string, number>
}

export interface DoctorReport {
  ok: boolean
  checks: { name: string; ok: boolean; detail: string }[]
  counts: Record<string, number>
}

/* ------------------------------------------------------------------- SSE */

export interface SseEvent {
  event: string
  data: Record<string, any>
}

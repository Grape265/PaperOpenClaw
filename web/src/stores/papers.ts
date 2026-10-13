/**
 * Paper catalogue cache.
 *
 * Shared by the library page and the paper selector so the selector does not
 * refetch the whole catalogue every time it is opened. Mutations (ingest,
 * delete) call `invalidate()` rather than trying to patch the cache in place,
 * which keeps the server the single source of truth.
 */

import { reactive } from 'vue'
import { papersApi, type Facets, type PaperQuery } from '../api/papers'
import type { PaperSummary } from '../api/types'

interface State {
  items: PaperSummary[]
  facets: Facets | null
  total: number
  page: number
  pageSize: number
  loading: boolean
  loaded: boolean
  error: string
  query: PaperQuery
}

const state = reactive<State>({
  items: [],
  facets: null,
  total: 0,
  page: 1,
  pageSize: 20,
  loading: false,
  loaded: false,
  error: '',
  query: { sort: '-created_at' },
})

export const papersStore = {
  state,

  async load(query: PaperQuery = {}, force = false): Promise<void> {
    const next = { ...state.query, ...query }
    const changed = JSON.stringify(next) !== JSON.stringify(state.query)
    if (state.loaded && !force && !changed) return

    state.query = next
    state.loading = true
    state.error = ''
    try {
      const data = await papersApi.list(next)
      state.items = data.items
      state.total = data.total
      state.page = data.page
      state.pageSize = data.page_size
      state.loaded = true
      if (!state.facets) {
        state.facets = await papersApi.facets().catch(() => null)
      }
    } catch (error) {
      state.error = (error as Error).message
    } finally {
      state.loading = false
    }
  },

  async refresh(): Promise<void> {
    state.facets = null
    await this.load(state.query, true)
  },

  async search(term: string): Promise<PaperSummary[]> {
    const data = await papersApi.list({ q: term, page_size: 50, sort: '-created_at' })
    return data.items
  },

  invalidate(): void {
    state.loaded = false
    state.facets = null
  },

  title(hash: string): string {
    const found = state.items.find((item) => item.hash === hash)
    return found ? found.title : hash.slice(0, 12)
  },

  byHash(hash: string): PaperSummary | undefined {
    return state.items.find((item) => item.hash === hash)
  },
}

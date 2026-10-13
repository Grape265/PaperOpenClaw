/**
 * Cross-page paper selection (FR-12.1).
 *
 * QA, brainstorm, graph and export all operate on "the selected papers", so the
 * selection lives here rather than in any one view. It survives navigation,
 * which is what makes the workflow feel like one application instead of eight
 * separate pages.
 */

import { computed, reactive } from 'vue'

interface State {
  hashes: string[]
  scope: 'selection' | 'all'
}

const state = reactive<State>({
  hashes: [],
  scope: 'all',
})

export const selectionStore = {
  state,

  get count(): number {
    return state.hashes.length
  },

  /** Hashes to send as a filter: null means "no filter, search everything". */
  get filter(): string[] | null {
    return state.hashes.length > 0 ? [...state.hashes] : null
  },

  get isActive(): boolean {
    return state.hashes.length > 0
  },

  has(hash: string): boolean {
    return state.hashes.includes(hash)
  },

  toggle(hash: string): void {
    const index = state.hashes.indexOf(hash)
    if (index >= 0) state.hashes.splice(index, 1)
    else state.hashes.push(hash)
  },

  add(hashes: string[]): void {
    for (const hash of hashes) {
      if (!state.hashes.includes(hash)) state.hashes.push(hash)
    }
  },

  remove(hashes: string[]): void {
    state.hashes = state.hashes.filter((hash) => !hashes.includes(hash))
  },

  set(hashes: string[]): void {
    state.hashes = [...new Set(hashes)]
  },

  clear(): void {
    state.hashes = []
  },

  /** Drop hashes that no longer exist (after a delete). */
  reconcile(existing: string[]): void {
    const alive = new Set(existing)
    state.hashes = state.hashes.filter((hash) => alive.has(hash))
  },

  summary(limit = 3): string {
    if (state.hashes.length === 0) return '未选择论文，将在全部论文中检索'
    const shown = state.hashes.slice(0, limit).map((hash) => hash.slice(0, 8))
    const rest = state.hashes.length - shown.length
    return rest > 0 ? `已选择 ${state.hashes.length} 篇：${shown.join('、')} 等` : `已选择：${shown.join('、')}`
  },
}

export const selectedCount = computed(() => state.hashes.length)

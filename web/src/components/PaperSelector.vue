<script setup lang="ts">
/**
 * Reusable multi-select paper picker (FR-12.1, FR-12.2).
 *
 * Shared by the library, QA, brainstorm, graph and export pages so the selection
 * behaves identically everywhere. Filtering hits the backend (`/papers`) rather
 * than filtering a cached page, so searching by author or hash prefix works
 * across the whole corpus, not just the rows currently displayed.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { papersApi } from '../api/papers'
import type { PaperSummary } from '../api/types'
import { papersStore } from '../stores/papers'
import { selectionStore } from '../stores/selection'
import { uiStore } from '../stores/ui'
import EmptyState from './EmptyState.vue'
import YearPicker from './YearPicker.vue'

const props = withDefaults(
  defineProps<{
    /** Hide the search/filter header when embedded in a page that has its own. */
    showFilters?: boolean
    pageSize?: number
    emptyHint?: string
  }>(),
  { showFilters: true, pageSize: 50, emptyHint: '先导入论文，或在设置中检查解析模型是否配置。' },
)

const term = ref('')
const journal = ref('')
const yearFrom = ref<number | null>(null)
const yearTo = ref<number | null>(null)
const status = ref('')
const loading = ref(false)
const items = ref<PaperSummary[]>([])
const total = ref(0)

let debounce: number | undefined

const selectedCount = computed(() => selectionStore.count)
const visibleHashes = computed(() => items.value.map((item) => item.hash))
const allVisibleSelected = computed(
  () => visibleHashes.value.length > 0 && visibleHashes.value.every((hash) => selectionStore.has(hash)),
)

async function load(): Promise<void> {
  loading.value = true
  try {
    const data = await papersApi.list({
      q: term.value || undefined,
      journal: journal.value || undefined,
      year_from: yearFrom.value,
      year_to: yearTo.value,
      status: status.value || undefined,
      page_size: props.pageSize,
      sort: '-created_at',
    })
    items.value = data.items
    total.value = data.total
    selectionStore.reconcile(data.items.map((item) => item.hash))
  } catch (error) {
    uiStore.error(`加载论文列表失败：${(error as Error).message}`)
  } finally {
    loading.value = false
  }
}

watch(term, () => {
  window.clearTimeout(debounce)
  debounce = window.setTimeout(load, 280)
})
watch([journal, yearFrom, yearTo, status], load)

onMounted(async () => {
  if (!papersStore.state.facets) {
    await papersStore.load({ page_size: props.pageSize }).catch(() => undefined)
  }
  await load()
})

function toggleAll(): void {
  if (allVisibleSelected.value) selectionStore.remove(visibleHashes.value)
  else selectionStore.add(visibleHashes.value)
}

function statusLabel(value: string): string {
  const map: Record<string, string> = {
    ready: '就绪',
    parsing: '解析中',
    indexing: '索引中',
    pending: '等待中',
    failed: '失败',
  }
  return map[value] ?? value
}

defineExpose({ reload: load })
</script>

<template>
  <div class="selector">
    <div v-if="showFilters" class="filters">
      <input
        v-model="term"
        type="search"
        class="grow"
        placeholder="搜索论文名、作者、期刊或哈希"
      />
      <select v-model="journal" class="filter-select">
        <option value="">全部期刊</option>
        <option v-for="name in papersStore.state.facets?.journals ?? []" :key="name" :value="name">
          {{ name }}
        </option>
      </select>
      <select v-model="status" class="filter-select">
        <option value="">全部状态</option>
        <option value="ready">就绪</option>
        <option value="parsing">解析中</option>
        <option value="indexing">索引中</option>
        <option value="failed">失败</option>
      </select>
      <YearPicker
        v-model="yearFrom"
        :min="papersStore.state.facets?.year_from ?? null"
        :max="papersStore.state.facets?.year_to ?? null"
        placeholder="起始年"
      />
      <YearPicker
        v-model="yearTo"
        :min="papersStore.state.facets?.year_from ?? null"
        :max="papersStore.state.facets?.year_to ?? null"
        placeholder="结束年"
      />
    </div>

    <div class="bar">
      <span class="dim">共 {{ total }} 篇，当前列出 {{ items.length }} 篇</span>
      <span class="grow" />
      <button class="btn btn-plain btn-sm" :disabled="items.length === 0" @click="toggleAll">
        {{ allVisibleSelected ? '取消全选' : '全选当前' }}
      </button>
      <button class="btn btn-plain btn-sm" :disabled="selectedCount === 0" @click="selectionStore.clear()">
        清空选择
      </button>
      <button class="btn btn-plain btn-sm" :disabled="loading" @click="load">
        {{ loading ? '加载中' : '刷新' }}
      </button>
    </div>

    <div v-if="items.length === 0 && !loading" class="empty-wrap">
      <EmptyState title="没有匹配的论文" :hint="emptyHint" />
    </div>

    <ul v-else class="paper-items">
      <li v-for="item in items" :key="item.hash" class="paper-item">
        <label class="checkbox paper-check">
          <input
            type="checkbox"
            :checked="selectionStore.has(item.hash)"
            @change="selectionStore.toggle(item.hash)"
          />
          <span class="box" />
        </label>
        <div class="paper-main">
          <div class="paper-title-row">
            <span class="paper-title">{{ item.title || '未命名论文' }}</span>
            <span class="chip" :class="item.status === 'ready' ? 'chip-ok' : item.status === 'failed' ? 'chip-danger' : ''">
              {{ statusLabel(item.status) }}
            </span>
            <span v-if="item.year" class="chip">{{ item.year }}</span>
          </div>
          <div class="paper-meta">
            <span v-if="item.journal">{{ item.journal }}</span>
            <span v-if="item.authors.length" class="truncate">
              {{ item.authors.map((a) => a.name).join('、') }}
            </span>
            <span class="mono">{{ item.hash_short }}</span>
          </div>
          <div class="paper-counts">
            <span>图 {{ item.counts.figures }}</span>
            <span>表 {{ item.counts.tables }}</span>
            <span>A {{ item.counts.rag_a }}</span>
            <span>B {{ item.counts.rag_b }}</span>
            <span>C {{ item.counts.rag_c }}</span>
            <span>图谱 {{ item.counts.kg_nodes }}</span>
          </div>
        </div>
        <div class="paper-actions">
          <slot name="row-actions" :paper="item" />
        </div>
      </li>
    </ul>

    <div v-if="selectedCount > 0" class="selection-bar">
      {{ selectionStore.summary() }}
    </div>
  </div>
</template>

<style scoped>
.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}

.filters input[type='search'] {
  min-width: 220px;
}

.filter-select {
  width: auto;
  min-width: 130px;
}

.bar {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  font-size: 12.5px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--glass-border);
}

.empty-wrap {
  padding: 10px 0;
}

.paper-items {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 420px;
  overflow-y: auto;
}

.paper-item {
  display: flex;
  gap: 10px;
  padding: 10px 4px;
  border-bottom: 1px solid var(--glass-border);
}

.paper-item:hover {
  background: var(--glass);
}

.paper-check {
  padding-top: 2px;
}

.paper-main {
  min-width: 0;
  flex: 1 1 auto;
}

.paper-title-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.paper-title {
  font-size: 13.5px;
  font-weight: 550;
}

.paper-meta {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--text-faint);
  margin-top: 3px;
}

.paper-counts {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  font-size: 11.5px;
  color: var(--text-faint);
  margin-top: 4px;
}

.paper-actions {
  flex: 0 0 auto;
  display: flex;
  align-items: flex-start;
  gap: 2px;
  flex-wrap: wrap;
  max-width: 190px;
  justify-content: flex-end;
}

.selection-bar {
  margin-top: 10px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--accent-soft);
  color: var(--accent-strong);
  font-size: 12.5px;
  line-height: 1.6;
}
</style>

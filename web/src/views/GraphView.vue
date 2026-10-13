<script setup lang="ts">
/**
 * 知识图谱页（关键词图）：
 *   - 数据来自 Knowledge Graph 库（从 OKF 用 LLM 提取的研究专业关键词 + 论文节点）；
 *   - 节点可拖拽移动、可缩放平移，论文名灰色；
 *   - 「展开论文节点」按钮：展开=论文+关键词全图；收起=只显示论文与论文之间的共享关键词连线；
 *   - 支持按论文名/哈希搜索定位。
 */
import { computed, onMounted, ref } from 'vue'
import GraphCanvas, { type CanvasEdge, type CanvasNode } from '../components/GraphCanvas.vue'
import { kgApi } from '../api/kg'
import { papersApi } from '../api/papers'
import type { PaperSummary } from '../api/types'
import { uiStore } from '../stores/ui'

const rawNodes = ref<CanvasNode[]>([])
const rawEdges = ref<CanvasEdge[]>([])
const loading = ref(true)
const selectedId = ref('')
const stats = ref<Record<string, number>>({})
const expanded = ref(true)

const searchTerm = ref('')
const searchResults = ref<PaperSummary[]>([])
const searching = ref(false)

async function loadGraph(): Promise<void> {
  loading.value = true
  try {
    const graph = await kgApi.graph({ limit: 5000 })
    rawNodes.value = graph.nodes.map((node) => ({
      id: node.key,
      label: node.label || (node.kind === 'paper' ? node.hash.slice(0, 8) : ''),
      kind: node.kind === 'paper' ? 'paper' : 'content',
      hash: node.hash || '',
      detail: node.detail || '',
    }))
    rawEdges.value = graph.edges.map((edge) => ({
      source: edge.source,
      target: edge.target,
      relation: edge.relation,
    }))
    stats.value = graph.stats ?? {}
  } catch (error) {
    uiStore.error(`加载知识图谱失败：${(error as Error).message}`)
  } finally {
    loading.value = false
  }
}

/** 收起模式下：两篇论文共享某个关键词，就在它们之间连一条线。 */
function paperLinks(): CanvasEdge[] {
  const keywordToPapers = new Map<string, string[]>()
  for (const edge of rawEdges.value) {
    if (edge.relation !== 'has_keyword') continue
    const arr = keywordToPapers.get(edge.target) ?? []
    if (!arr.includes(edge.source)) arr.push(edge.source)
    keywordToPapers.set(edge.target, arr)
  }
  const links: CanvasEdge[] = []
  const seen = new Set<string>()
  for (const papers of keywordToPapers.values()) {
    if (papers.length < 2) continue
    for (let i = 0; i < papers.length; i++) {
      for (let j = i + 1; j < papers.length; j++) {
        const key = [papers[i], papers[j]].sort().join('|')
        if (seen.has(key)) continue
        seen.add(key)
        links.push({ source: papers[i], target: papers[j], relation: 'shared' })
      }
    }
  }
  return links
}

const displayNodes = computed<CanvasNode[]>(() => {
  if (expanded.value) return rawNodes.value
  return rawNodes.value.filter((node) => node.kind === 'paper')
})

const displayEdges = computed<CanvasEdge[]>(() => {
  if (expanded.value) return rawEdges.value.filter((edge) => edge.relation === 'has_keyword')
  return paperLinks()
})

function clickNode(node: CanvasNode): void {
  selectedId.value = node.id
}

async function search(): Promise<void> {
  const term = searchTerm.value.trim()
  if (!term) {
    searchResults.value = []
    return
  }
  searching.value = true
  try {
    const data = await papersApi.list({ q: term, page_size: 20, sort: '-created_at' })
    searchResults.value = data.items
  } catch (error) {
    uiStore.error(`搜索失败：${(error as Error).message}`)
  } finally {
    searching.value = false
  }
}

function locate(result: PaperSummary): void {
  selectedId.value = `paper:${result.hash}`
  searchResults.value = []
  searchTerm.value = result.title || result.hash_short
}

function exportGraph(format: string): void {
  window.open(kgApi.exportUrl({ format }), '_blank')
}

onMounted(loadGraph)
</script>

<template>
  <div class="col">
    <section class="glass card">
      <div class="card-head">
        <div>
          <div class="card-title">知识图谱</div>
          <div class="card-subtitle">
            关键词从 OKF 用 LLM 提取（仅研究专业词）；可拖拽节点、缩放平移
          </div>
        </div>
        <div class="btn-row">
          <button class="btn btn-ghost btn-sm" @click="expanded = !expanded">
            {{ expanded ? '收起论文节点' : '展开论文节点' }}
          </button>
          <input
            v-model="searchTerm"
            type="search"
            class="search-input"
            placeholder="搜索论文名、作者或哈希"
            @keydown.enter="search"
            @keydown.esc="searchResults = []; searchTerm = ''"
          />
          <button class="btn btn-ghost btn-sm" :disabled="searching" @click="search">
            {{ searching ? '搜索中' : '搜索' }}
          </button>
          <button class="btn btn-plain btn-sm" @click="exportGraph('json')">导出 JSON</button>
          <button class="btn btn-plain btn-sm" @click="exportGraph('graphml')">导出 GraphML</button>
        </div>
      </div>

      <div v-if="searchResults.length" class="search-results">
        <div v-for="result in searchResults" :key="result.hash" class="search-result">
          <span class="grow truncate">{{ result.title || '未命名论文' }}</span>
          <span class="mono faint">{{ result.hash_short }}</span>
          <button class="btn btn-plain btn-sm" @click="locate(result)">定位</button>
        </div>
      </div>
    </section>

    <section class="glass card">
      <div v-if="loading" class="empty">正在加载知识图谱</div>
      <div v-else-if="displayNodes.length === 0" class="empty">
        <div>知识图谱为空</div>
        <p class="field-hint">导入论文后，这里会显示论文气泡与它们共享的关键词连线。</p>
      </div>
      <GraphCanvas
        v-else
        :nodes="displayNodes"
        :edges="displayEdges"
        :selected-id="selectedId"
        seed="circle"
        :height="620"
        @node-click="clickNode"
      />
    </section>

    <section class="glass card row-between wrap">
      <div class="legend">
        <span class="legend-item"><i class="dot dot-paper" /> 论文</span>
        <span class="legend-item"><i class="dot dot-keyword" /> 关键词</span>
        <span v-if="!expanded" class="legend-item"><i class="line-sample" /> 共享关键词</span>
      </div>
      <span class="faint">
        节点 {{ stats.nodes ?? 0 }} · 边 {{ stats.edges ?? 0 }} · 悬停显示论文名/关键词 ·
        拖拽节点移动，滚轮缩放
      </span>
    </section>
  </div>
</template>

<style scoped>
.search-input {
  width: 220px;
}

.legend {
  display: flex;
  gap: 14px;
  align-items: center;
  flex-wrap: wrap;
}

.legend-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-dim);
}

.dot {
  display: inline-block;
  width: 11px;
  height: 11px;
  border-radius: 50%;
}

.dot-paper { background: var(--accent); }
.dot-keyword { background: var(--accent-soft); border: 1px solid var(--accent); }

.line-sample {
  display: inline-block;
  width: 16px;
  height: 2px;
  background: var(--text-faint);
}

.search-results {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 8px;
  padding: 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  max-height: 240px;
  overflow-y: auto;
}

.search-result {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 5px 6px;
  border-radius: var(--radius-sm);
  font-size: 13px;
}

.search-result:hover { background: var(--bg-hover); }
</style>

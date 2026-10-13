<script setup lang="ts">
/**
 * Library page (FR-12.1, FR-12.2).
 *
 * The list itself comes from the shared PaperSelector; this page adds the
 * corpus-level actions: open, export, re-parse, delete, and bulk operations on
 * the current selection.
 */
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import PaperSelector from '../components/PaperSelector.vue'
import { papersApi } from '../api/papers'
import { kgApi } from '../api/kg'
import type { PaperSummary } from '../api/types'
import { selectionStore } from '../stores/selection'
import { papersStore } from '../stores/papers'
import { uiStore } from '../stores/ui'

const router = useRouter()
const selector = ref<InstanceType<typeof PaperSelector> | null>(null)
const busy = ref('')

const selected = computed(() => selectionStore.count)

function open(paper: PaperSummary): void {
  void router.push(`/paper/${paper.hash}`)
}

async function remove(paper: PaperSummary): Promise<void> {
  const label = paper.title || paper.hash_short
  if (!window.confirm(`确认删除《${label}》？将同时清理其向量、图谱与 OKF 文件，且不可恢复。`)) return
  busy.value = paper.hash
  try {
    await papersApi.remove(paper.hash)
    selectionStore.remove([paper.hash])
    papersStore.invalidate()
    uiStore.ok(`已删除《${label}》`)
    await selector.value?.reload()
  } catch (error) {
    uiStore.error(`删除失败：${(error as Error).message}`)
  } finally {
    busy.value = ''
  }
}

async function removeSelected(): Promise<void> {
  const hashes = [...selectionStore.state.hashes]
  if (hashes.length === 0) return
  if (!window.confirm(`确认删除选中的 ${hashes.length} 篇论文？该操作不可恢复。`)) return
  busy.value = 'bulk'
  let done = 0
  for (const hash of hashes) {
    try {
      await papersApi.remove(hash)
      done += 1
    } catch (error) {
      uiStore.error(`删除 ${hash.slice(0, 8)} 失败：${(error as Error).message}`)
    }
  }
  selectionStore.clear()
  papersStore.invalidate()
  uiStore.ok(`已删除 ${done}/${hashes.length} 篇论文`)
  busy.value = ''
  await selector.value?.reload()
}

async function reparse(paper: PaperSummary): Promise<void> {
  busy.value = paper.hash
  try {
    const data = await papersApi.reparse(paper.hash)
    uiStore.ok(`已提交重新解析任务：${data.job_id}`)
    void router.push('/ingest')
  } catch (error) {
    uiStore.error(`提交失败：${(error as Error).message}`)
  } finally {
    busy.value = ''
  }
}

async function reindex(): Promise<void> {
  busy.value = 'reindex'
  try {
    const data = await ragRebuild()
    uiStore.ok(`已提交向量重建任务：${data.job_id}`)
    void router.push('/ingest')
  } catch (error) {
    uiStore.error(`提交失败：${(error as Error).message}`)
  } finally {
    busy.value = ''
  }
}

async function ragRebuild(): Promise<{ job_id: string }> {
  const { ragApi } = await import('../api/rag')
  return ragApi.rebuild({ hashes: selectionStore.filter ?? undefined })
}

function exportSelected(): void {
  const hashes = selectionStore.filter
  window.open(kgApi.exportUrl({ hashes: hashes ?? undefined, format: 'json' }), '_blank')
}

function exportPaper(paper: PaperSummary): void {
  window.open(papersApi.exportUrl(paper.hash), '_blank')
}
</script>

<template>
  <div class="col">
    <section class="glass card">
      <div class="card-head">
        <div>
          <div class="card-title">论文库</div>
          <div class="card-subtitle">
            勾选论文后，问答、头脑风暴与图谱页面会默认限定在这些论文上
          </div>
        </div>
        <div class="btn-row">
          <button class="btn btn-primary" @click="router.push('/ingest')">导入论文</button>
          <button class="btn btn-ghost" :disabled="selected === 0" @click="exportSelected">
            导出所选图谱
          </button>
          <button class="btn btn-ghost" :disabled="busy === 'reindex'" @click="reindex">
            重建向量库
          </button>
          <button class="btn btn-danger" :disabled="selected === 0 || busy === 'bulk'" @click="removeSelected">
            删除所选
          </button>
        </div>
      </div>

      <PaperSelector ref="selector" :page-size="50">
        <template #row-actions="{ paper }">
          <button class="btn btn-plain btn-sm" @click="open(paper)">查看</button>
          <button class="btn btn-plain btn-sm" :disabled="busy === paper.hash" @click="reparse(paper)">
            重解析
          </button>
          <button class="btn btn-plain btn-sm" @click="exportPaper(paper)">导出</button>
          <button class="btn btn-plain btn-sm danger-text" :disabled="busy === paper.hash" @click="remove(paper)">
            删除
          </button>
        </template>
      </PaperSelector>
    </section>
  </div>
</template>

<style scoped>
.danger-text {
  color: var(--danger);
}
</style>

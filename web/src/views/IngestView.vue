<script setup lang="ts">
/**
 * Ingest page: upload PDFs and watch job progress.
 *
 * Progress is driven by the job SSE stream (FR-1.5); the list is refreshed from
 * `/jobs` so a page reload still shows the true state. Only PDF upload is
 * offered — no server-side path scanning.
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ingestApi } from '../api/ingest'
import type { IngestItem, Job } from '../api/types'
import { papersStore } from '../stores/papers'
import { uiStore } from '../stores/ui'
import EmptyState from '../components/EmptyState.vue'

const files = ref<File[]>([])
const dragging = ref(false)
const uploading = ref(false)
const useLlama = ref(true)
const results = ref<IngestItem[]>([])
const jobs = ref<Job[]>([])
const closers = new Map<string, () => void>()
const fileInput = ref<HTMLInputElement | null>(null)

const runningCount = computed(() => jobs.value.filter((job) => job.status === 'running' || job.status === 'pending').length)

function pick(event: Event): void {
  const target = event.target as HTMLInputElement
  if (target.files) addFiles(Array.from(target.files))
  target.value = ''
}

function addFiles(incoming: File[]): void {
  const pdfs = incoming.filter((file) => file.name.toLowerCase().endsWith('.pdf'))
  if (pdfs.length !== incoming.length) uiStore.warn('已忽略非 PDF 文件')
  for (const file of pdfs) {
    if (!files.value.some((existing) => existing.name === file.name && existing.size === file.size)) {
      files.value.push(file)
    }
  }
}

function onDrop(event: DragEvent): void {
  dragging.value = false
  const dropped = event.dataTransfer?.files
  if (dropped) addFiles(Array.from(dropped))
}

async function upload(): Promise<void> {
  if (files.value.length === 0) return
  uploading.value = true
  try {
    const data = await ingestApi.upload(files.value, useLlama.value)
    results.value = [...data.items, ...results.value]
    uiStore.ok(`已接收 ${data.accepted} 个文件，去重跳过 ${data.duplicated} 个`)
    files.value = []
    for (const item of data.items) {
      if (item.job_id) watch(item.job_id)
    }
    papersStore.invalidate()
    await loadJobs()
  } catch (error) {
    uiStore.error(`上传失败：${(error as Error).message}`)
  } finally {
    uploading.value = false
  }
}

async function loadJobs(): Promise<void> {
  try {
    const data = await ingestApi.jobs({ page_size: 100 })
    jobs.value = data.items
  } catch (error) {
    uiStore.error(`加载任务失败：${(error as Error).message}`)
  }
}

async function deleteJob(job: Job): Promise<void> {
  try {
    await ingestApi.deleteJob(job.id)
    jobs.value = jobs.value.filter((item) => item.id !== job.id)
    uiStore.ok('已删除导入记录')
  } catch (error) {
    uiStore.error(`删除失败：${(error as Error).message}`)
  }
}

function exportCsv(): void {
  window.open(ingestApi.exportUrl(), '_blank')
}

function watch(jobId: string): void {
  if (closers.has(jobId)) return
  const close = ingestApi.watchJob(jobId, {
    onStage: (data) => patch(jobId, { message: data.message, progress: data.progress }),
    onProgress: (data) => patch(jobId, { message: data.message, progress: data.progress }),
    onLog: (data) => patch(jobId, { message: data.message }),
    onDone: (data) => {
      patch(jobId, { status: String(data.status ?? 'done') })
      closers.get(jobId)?.()
      closers.delete(jobId)
      void loadJobs()
      papersStore.invalidate()
    },
    onError: (data) => {
      patch(jobId, { status: 'failed', error: String(data.message ?? '') })
      closers.get(jobId)?.()
      closers.delete(jobId)
      uiStore.error(`任务失败：${data.message ?? ''}`)
      void loadJobs()
    },
  })
  closers.set(jobId, close)
}

function patch(jobId: string, changes: Partial<Job>): void {
  const index = jobs.value.findIndex((job) => job.id === jobId)
  if (index >= 0) jobs.value[index] = { ...jobs.value[index], ...changes }
}

async function cancel(job: Job): Promise<void> {
  try {
    await ingestApi.cancel(job.id)
    patch(job.id, { status: 'cancelled' })
    uiStore.warn('已请求取消任务')
  } catch (error) {
    uiStore.error(`取消失败：${(error as Error).message}`)
  }
}

function stageLabel(job: Job): string {
  const map: Record<string, string> = {
    receive: '接收文件',
    layout: '版面分析与图表裁剪',
    vision: '大模型解析',
    okf: 'OKF 落盘',
    pyramid: '金字塔 RAG 构建',
    kg: '知识图谱构建',
    finalize: '完成入库',
  }
  return map[job.stage] ?? job.stage
}

function statusLabel(status: string): string {
  const map: Record<string, string> = {
    pending: '排队中',
    running: '进行中',
    done: '已完成',
    failed: '失败',
    cancelled: '已取消',
  }
  return map[status] ?? status
}

onMounted(async () => {
  await loadJobs()
  for (const job of jobs.value) {
    if (job.status === 'running' || job.status === 'pending') watch(job.id)
  }
})

onUnmounted(() => {
  for (const close of closers.values()) close()
  closers.clear()
})
</script>

<template>
  <div class="col">
    <section class="glass card">
      <div class="card-head">
        <div>
          <div class="card-title">上传 PDF</div>
          <div class="card-subtitle">
            支持一次选择多个文件；重复论文会按哈希自动跳过。关闭「使用 LLM 解析」则直接用本地 PyMuPDF 解析（快）
          </div>
        </div>
        <div class="btn-row">
          <label class="checkbox">
            <input v-model="useLlama" type="checkbox" />
            <span class="box" />
            使用 LLM 解析
          </label>
          <button class="btn btn-ghost" @click="fileInput?.click()">选择文件</button>
          <button class="btn btn-primary" :disabled="files.length === 0 || uploading" @click="upload">
            {{ uploading ? '处理中' : `开始导入（${files.length}）` }}
          </button>
        </div>
      </div>

      <input ref="fileInput" type="file" accept="application/pdf" multiple hidden @change="pick" />

      <div
        class="dropzone"
        :class="{ dragging }"
        @dragover.prevent="dragging = true"
        @dragleave.prevent="dragging = false"
        @drop.prevent="onDrop"
      >
        将 PDF 文件拖到此处，或点击「选择文件」
      </div>

      <ul v-if="files.length" class="file-list">
        <li v-for="(file, index) in files" :key="`${file.name}-${index}`" class="file-item">
          <span class="grow truncate">{{ file.name }}</span>
          <span class="faint">{{ (file.size / 1024 / 1024).toFixed(1) }} MB</span>
          <button class="btn btn-plain btn-sm" @click="files.splice(index, 1)">移除</button>
        </li>
      </ul>
    </section>

    <section class="glass card">
      <div class="card-head">
        <div>
          <div class="card-title">任务</div>
          <div class="card-subtitle">
            七阶段流水线：接收 → 版面分析 → 大模型解析 → OKF → 金字塔 RAG → 图谱 → 完成
          </div>
        </div>
        <div class="btn-row">
          <span v-if="runningCount" class="chip chip-accent">{{ runningCount }} 个进行中</span>
          <button class="btn btn-ghost" @click="exportCsv">导出 CSV</button>
          <button class="btn btn-ghost" @click="loadJobs">刷新</button>
        </div>
      </div>

      <EmptyState v-if="jobs.length === 0" title="暂无任务" hint="导入 PDF 后，这里会显示每个阶段的进度。" />

      <ul v-else class="job-list">
        <li v-for="job in jobs" :key="job.id" class="job-item">
          <div class="row-between">
            <div class="grow">
              <div class="job-title">
                {{ job.filename || job.hash.slice(0, 12) || job.kind }}
                <span
                  class="chip"
                  :class="job.status === 'done' ? 'chip-ok' : job.status === 'failed' ? 'chip-danger' : 'chip-accent'"
                >
                  {{ statusLabel(job.status) }}
                </span>
                <span class="chip">{{ job.kind }}</span>
              </div>
              <div class="job-meta">
                <span>{{ stageLabel(job) }}</span>
                <span v-if="job.message" class="truncate">{{ job.message }}</span>
                <span v-if="job.usage?.total_tokens" class="faint">tokens {{ job.usage.total_tokens }}</span>
              </div>
            </div>
            <div class="btn-row">
              <button
                v-if="job.status === 'running' || job.status === 'pending'"
                class="btn btn-plain btn-sm"
                @click="cancel(job)"
              >
                取消
              </button>
              <button class="btn btn-plain btn-sm" @click="deleteJob(job)">删除</button>
            </div>
          </div>
          <div class="progress" style="margin-top: 8px">
            <i :style="{ width: `${Math.round((job.progress || 0) * 100)}%` }" />
          </div>
          <div v-if="job.error" class="alert alert-error" style="margin: 8px 0 0">{{ job.error }}</div>
          <details v-if="job.stages.length" class="stages">
            <summary>阶段耗时</summary>
            <ul>
              <li v-for="stage in job.stages" :key="stage.stage">
                <span>{{ stageLabel({ stage: stage.stage } as Job) }}</span>
                <span class="faint">
                  {{ stage.duration_ms !== null ? `${(stage.duration_ms / 1000).toFixed(1)}s` : '进行中' }}
                </span>
                <span v-if="stage.message" class="faint truncate">{{ stage.message }}</span>
              </li>
            </ul>
          </details>
        </li>
      </ul>
    </section>

    <section v-if="results.length" class="glass card">
      <div class="card-head">
        <div class="card-title">本次导入结果</div>
        <button class="btn btn-plain btn-sm" @click="results = []">清空</button>
      </div>
      <table class="table">
        <thead>
          <tr>
            <th>文件</th>
            <th>哈希</th>
            <th>结果</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(item, index) in results" :key="index">
            <td>{{ item.filename }}</td>
            <td class="mono">{{ item.hash_short ?? '—' }}</td>
            <td>
              <span v-if="item.error" class="chip chip-danger">{{ item.error }}</span>
              <span v-else-if="item.duplicated" class="chip">已存在，跳过</span>
              <span v-else class="chip chip-ok">已排队</span>
            </td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>

<style scoped>
.dropzone {
  padding: 28px 18px;
  border: 1.5px dashed var(--glass-border-strong);
  border-radius: var(--radius-md);
  text-align: center;
  font-size: 13px;
  color: var(--text-faint);
  transition: border-color var(--transition), background var(--transition);
}

.dropzone.dragging {
  border-color: var(--accent);
  background: var(--accent-soft);
  color: var(--accent-strong);
}

.file-list {
  list-style: none;
  margin: 12px 0 0;
  padding: 0;
}

.file-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 2px;
  border-bottom: 1px solid var(--glass-border);
  font-size: 13px;
}

.job-list {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 520px;
  overflow-y: auto;
  border-top: 1px solid var(--glass-border);
}

.job-item {
  padding: 12px 2px;
  border-bottom: 1px solid var(--glass-border);
}

.job-title {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  font-size: 13.5px;
  font-weight: 550;
}

.job-meta {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--text-faint);
  margin-top: 4px;
}

.stages {
  margin-top: 8px;
  font-size: 12px;
  color: var(--text-dim);
}

.stages summary {
  cursor: pointer;
  color: var(--text-faint);
}

.stages ul {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
}

.stages li {
  display: flex;
  gap: 12px;
  padding: 3px 0;
}
</style>

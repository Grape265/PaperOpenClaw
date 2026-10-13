<script setup lang="ts">
/**
 * Paper detail page (FR-8).
 *
 * Six views of one paper, all backed by real endpoints: the metadata record, the
 * original PDF, the OKF bundle, the cropped figures/tables, the A/B/C pyramid and
 * the fishbone graph.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { papersApi } from '../api/papers'
import { kgApi, type FishboneResponse } from '../api/kg'
import { ragApi } from '../api/rag'
import type { OkfDocument, OkfTreeNode, PaperDetail } from '../api/types'
import MarkdownView from '../components/MarkdownView.vue'
import EmptyState from '../components/EmptyState.vue'
import { uiStore } from '../stores/ui'
import { selectionStore } from '../stores/selection'

const route = useRoute()
const router = useRouter()

const hash = computed(() => String(route.params.hash ?? ''))
const detail = ref<PaperDetail | null>(null)
const loading = ref(true)
const error = ref('')
const tab = ref<'overview' | 'pdf' | 'okf' | 'assets' | 'pyramid' | 'fishbone'>('overview')

const okfDoc = ref<OkfDocument | null>(null)
const okfError = ref('')
const okfReport = ref<string>('')
const fishbone = ref<FishboneResponse | null>(null)
const zoomed = ref<{ label: string; caption: string } | null>(null)

const tabs = [
  { id: 'overview', label: '概览' },
  { id: 'pdf', label: '原文' },
  { id: 'okf', label: 'OKF' },
  { id: 'assets', label: '图表' },
  { id: 'pyramid', label: '金字塔 RAG' },
  { id: 'fishbone', label: '鱼骨图谱' },
] as const

const figures = computed(() => (detail.value?.assets ?? []).filter((item) => item.kind === 'figure'))
const tables = computed(() => (detail.value?.assets ?? []).filter((item) => item.kind === 'table'))

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    detail.value = await papersApi.get(hash.value)
  } catch (caught) {
    error.value = (caught as Error).message
  } finally {
    loading.value = false
  }
}

async function loadFishbone(): Promise<void> {
  if (fishbone.value) return
  try {
    fishbone.value = await kgApi.fishbone(hash.value)
  } catch (caught) {
    uiStore.error(`加载鱼骨图谱失败：${(caught as Error).message}`)
  }
}

async function openConcept(conceptId: string): Promise<void> {
  okfError.value = ''
  if (conceptId.endsWith('/')) return
  try {
    okfDoc.value = await papersApi.okfConcept(hash.value, conceptId)
  } catch (caught) {
    okfDoc.value = null
    okfError.value = (caught as Error).message
  }
}

/** 把 OKF 正文里的相对链接解析成目标：概念 ID / PDF / 图片标签。 */
function resolveOkfLink(currentConceptId: string, href: string): string {
  let segments: string[]
  if (href.startsWith('/')) {
    segments = href.slice(1).split('/')
  } else {
    const dir = currentConceptId.split('/').slice(0, -1)
    segments = [...dir, ...href.split('/')]
  }
  const resolved: string[] = []
  for (const segment of segments) {
    if (segment === '' || segment === '.') continue
    if (segment === '..') resolved.pop()
    else resolved.push(segment)
  }
  return resolved.join('/')
}

/** OKF 内链点击：显示 PDF、图片或跳到另一个概念，而不是交给路由器跳转。 */
function onOkfLink(href: string): void {
  if (href.endsWith('.pdf') || href.toLowerCase().includes('.pdf')) {
    tab.value = 'pdf'
    return
  }
  if (href.endsWith('.png')) {
    const filename = href.split('/').pop() || ''
    const label = filename.replace(/\.png$/i, '')
    const asset = (detail.value?.assets ?? []).find((item) => item.label === label)
    zoomed.value = { label, caption: asset?.caption ?? label }
    return
  }
  if (href.endsWith('.md')) {
    const conceptId = resolveOkfLink(okfDoc.value?.concept_id ?? '', href)
    void openConcept(conceptId)
    return
  }
}

async function verifyOkf(): Promise<void> {
  okfReport.value = '校验中'
  try {
    const report = await papersApi.okfVerify(hash.value)
    okfReport.value = report.conformant
      ? `符合 OKF v${report.okf_version} 规范，共 ${report.files} 个概念文档；警告 ${report.warnings.length} 条`
      : `不符合规范：${report.errors.length} 个错误，警告 ${report.warnings.length} 条`
  } catch (caught) {
    okfReport.value = `校验失败：${(caught as Error).message}`
  }
}

function flatten(nodes: OkfTreeNode[], depth = 0): { node: OkfTreeNode; depth: number }[] {
  const out: { node: OkfTreeNode; depth: number }[] = []
  for (const node of nodes) {
    out.push({ node, depth })
    if (node.children?.length) out.push(...flatten(node.children, depth + 1))
  }
  return out
}

function openAsset(label: string, caption: string): void {
  zoomed.value = { label, caption }
}

/** Normalised access to layer-B points, which live in the unit's JSON meta. */
function pointsOf(unit: { meta?: Record<string, unknown> }): { text: string; assets: string[] }[] {
  const raw = unit.meta?.points
  if (!Array.isArray(raw)) return []
  return raw
    .filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null)
    .map((item) => ({
      text: String(item.text ?? ''),
      assets: Array.isArray(item.assets) ? item.assets.map(String) : [],
    }))
}

function openExport(): void {
  window.open(papersApi.exportUrl(hash.value), '_blank')
}

function openInNewTab(url: string): void {
  window.open(url, '_blank')
}

async function removePaper(): Promise<void> {
  const title = detail.value?.paper.title ?? hash.value.slice(0, 8)
  if (!window.confirm(`确认删除《${title}》？该操作不可恢复。`)) return
  try {
    await papersApi.remove(hash.value)
    selectionStore.remove([hash.value])
    uiStore.ok('已删除论文')
    void router.push('/library')
  } catch (caught) {
    uiStore.error(`删除失败：${(caught as Error).message}`)
  }
}

watch(tab, (value) => {
  if (value === 'fishbone') void loadFishbone()
})
watch(hash, () => {
  fishbone.value = null
  okfDoc.value = null
  void load()
})

onMounted(load)
</script>

<template>
  <div class="col">
    <div v-if="loading" class="glass card dim">正在加载论文详情</div>
    <div v-else-if="error" class="glass card">
      <div class="alert alert-error">{{ error }}</div>
      <button class="btn btn-ghost" @click="router.push('/library')">返回论文库</button>
    </div>

    <template v-else-if="detail">
      <section class="glass card">
        <div class="card-head">
          <div class="grow">
            <h2 class="paper-title">{{ detail.paper.title || '未命名论文' }}</h2>
            <div class="paper-meta">
              <span v-if="detail.paper.journal" class="chip">{{ detail.paper.journal }}</span>
              <span v-if="detail.paper.year" class="chip">{{ detail.paper.year }}</span>
              <span v-if="detail.paper.published_at" class="chip">{{ detail.paper.published_at }}</span>
              <span class="chip mono">{{ detail.paper.hash_short }}</span>
              <span class="chip">{{ detail.paper.language }}</span>
              <span class="chip">{{ detail.paper.page_count }} 页</span>
              <span
                class="chip"
                :class="detail.paper.status === 'ready' ? 'chip-ok' : detail.paper.status === 'failed' ? 'chip-danger' : ''"
              >
                {{ detail.paper.status }}
              </span>
            </div>
          </div>
          <div class="btn-row">
            <button class="btn btn-ghost" @click="router.push('/library')">返回列表</button>
            <button class="btn btn-ghost" @click="openExport()">导出压缩包</button>
            <button class="btn btn-danger" @click="removePaper">删除</button>
          </div>
        </div>
        <div v-if="detail.paper.error" class="alert alert-error">{{ detail.paper.error }}</div>
      </section>

      <nav class="glass tabs">
        <button
          v-for="item in tabs"
          :key="item.id"
          class="tab"
          :class="{ active: tab === item.id }"
          @click="tab = item.id"
        >
          {{ item.label }}
        </button>
      </nav>

      <!-- ---------------------------------------------------- overview -->
      <section v-if="tab === 'overview'" class="col">
        <div class="glass card">
          <div class="card-head"><div class="card-title">摘要</div></div>
          <p class="abstract">{{ detail.abstract || '（原文未提供摘要）' }}</p>
          <div v-if="detail.paper.keywords.length" class="chip-row">
            <span v-for="word in detail.paper.keywords" :key="word" class="chip">{{ word }}</span>
          </div>
        </div>

        <div class="glass card">
          <div class="card-head"><div class="card-title">作者与单位</div></div>
          <EmptyState v-if="detail.paper.authors.length === 0" title="未抽取到作者信息" />
          <table v-else class="table">
            <thead>
              <tr><th>序号</th><th>姓名</th><th>单位</th><th>通讯</th><th>邮箱</th></tr>
            </thead>
            <tbody>
              <tr v-for="(author, index) in detail.paper.authors" :key="index">
                <td>{{ index + 1 }}</td>
                <td>{{ author.name }}</td>
                <td>{{ author.affiliations.join('；') || '—' }}</td>
                <td>{{ author.is_corresponding ? '是' : '' }}</td>
                <td class="mono">{{ author.email || '' }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="glass card">
          <div class="card-head"><div class="card-title">统计</div></div>
          <div class="stat-grid">
            <div class="stat"><span class="faint">图</span><b>{{ detail.paper.counts.figures }}</b></div>
            <div class="stat"><span class="faint">表</span><b>{{ detail.paper.counts.tables }}</b></div>
            <div class="stat"><span class="faint">OKF 概念</span><b>{{ detail.okf.concepts.length }}</b></div>
            <div class="stat"><span class="faint">A 层大意</span><b>{{ detail.pyramid.counts.A }}</b></div>
            <div class="stat"><span class="faint">B 层要点</span><b>{{ detail.pyramid.counts.B }}</b></div>
            <div class="stat"><span class="faint">C 层摘要</span><b>{{ detail.pyramid.counts.C }}</b></div>
            <div class="stat"><span class="faint">图谱节点</span><b>{{ detail.kg_summary.nodes }}</b></div>
            <div class="stat"><span class="faint">已覆盖维度</span><b>{{ detail.kg_summary.sections.length }}/7</b></div>
          </div>
        </div>

        <div class="glass card">
          <div class="card-head">
            <div class="card-title">入库快照</div>
            <div class="card-subtitle">
              模型：{{ detail.ingest?.provider || '—' }} / {{ detail.ingest?.model || '—' }}
            </div>
          </div>
          <pre>{{ JSON.stringify(detail.ingest?.counts ?? detail.ingest, null, 2) }}</pre>
          <div v-if="detail.ingest?.parse_warnings?.length" class="alert alert-warn">
            <div v-for="(warning, index) in detail.ingest.parse_warnings" :key="index">{{ warning }}</div>
          </div>
        </div>
      </section>

      <!-- --------------------------------------------------------- pdf -->
      <section v-else-if="tab === 'pdf'" class="glass card">
        <div class="card-head">
          <div class="card-title">原文 PDF</div>
          <button class="btn btn-ghost" @click="openInNewTab(papersApi.pdfUrl(hash))">
            在新窗口打开
          </button>
        </div>
        <iframe class="pdf-frame" :src="papersApi.pdfUrl(hash)" title="论文原文" />
      </section>

      <!-- --------------------------------------------------------- okf -->
      <section v-else-if="tab === 'okf'" class="okf-layout">
        <div class="glass card okf-tree">
          <div class="card-head">
            <div class="card-title">OKF v0.2</div>
            <button class="btn btn-plain btn-sm" @click="verifyOkf">校验</button>
          </div>
          <p v-if="okfReport" class="field-hint">{{ okfReport }}</p>
          <EmptyState
            v-if="!detail.okf.concepts.length"
            title="尚未生成 OKF 文档"
            hint="重新解析该论文即可生成 OKF Bundle。"
          />
          <ul v-else class="concept-list">
            <li
              v-for="(entry, index) in detail.okf.concepts"
              :key="entry.concept_id + index"
              class="concept-item"
            >
              <button class="concept-link" @click="openConcept(entry.concept_id)">
                <span class="concept-type">{{ entry.type }}</span>
                <span class="truncate">{{ entry.title || entry.concept_id }}</span>
              </button>
            </li>
          </ul>
        </div>

        <div class="glass card okf-body">
          <div v-if="okfError" class="alert alert-error">{{ okfError }}</div>
          <EmptyState v-else-if="!okfDoc" title="选择一个概念查看内容" hint="左侧列出 OKF Bundle 中的全部概念文档。" />
          <template v-else>
            <div class="card-head">
              <div>
                <div class="card-title">{{ okfDoc.frontmatter.title || okfDoc.concept_id }}</div>
                <div class="card-subtitle mono">{{ okfDoc.file_path }}</div>
              </div>
              <span class="chip chip-accent">{{ okfDoc.frontmatter.type }}</span>
            </div>
            <details class="frontmatter">
              <summary>Frontmatter</summary>
              <pre>{{ JSON.stringify(okfDoc.frontmatter, null, 2) }}</pre>
            </details>
            <MarkdownView :source="okfDoc.body" @link-click="onOkfLink" />
            <div v-if="okfDoc.links.length" class="chip-row" style="margin-top: 12px">
              <span v-for="link in okfDoc.links" :key="link" class="chip mono">{{ link }}</span>
            </div>
          </template>
        </div>
      </section>

      <!-- ------------------------------------------------------ assets -->
      <section v-else-if="tab === 'assets'" class="col">
        <div class="glass card">
          <div class="card-head">
            <div class="card-title">图（{{ figures.length }}）</div>
            <div class="card-subtitle">存放在 papers/&lt;哈希&gt;/fig/</div>
          </div>
          <EmptyState v-if="figures.length === 0" title="未裁剪出任何图" />
          <div v-else class="asset-grid">
            <figure v-for="asset in figures" :key="asset.label" class="asset">
              <img :src="papersApi.assetUrl(hash, asset.label)" :alt="asset.label" @click="openAsset(asset.label, asset.caption)" />
              <figcaption>
                <div class="asset-head">
                  <span class="chip chip-accent">{{ asset.label }}</span>
                  <span class="chip">第 {{ asset.page }} 页</span>
                  <span v-if="asset.section" class="chip">{{ asset.section }}</span>
                </div>
                <p class="asset-caption">{{ asset.caption || '（未识别到图注）' }}</p>
              </figcaption>
            </figure>
          </div>
        </div>

        <div class="glass card">
          <div class="card-head">
            <div class="card-title">表（{{ tables.length }}）</div>
            <div class="card-subtitle">存放在 papers/&lt;哈希&gt;/tab/</div>
          </div>
          <EmptyState v-if="tables.length === 0" title="未裁剪出任何表" />
          <div v-else class="asset-grid">
            <figure v-for="asset in tables" :key="asset.label" class="asset">
              <img :src="papersApi.assetUrl(hash, asset.label)" :alt="asset.label" @click="openAsset(asset.label, asset.caption)" />
              <figcaption>
                <div class="asset-head">
                  <span class="chip chip-accent">{{ asset.label }}</span>
                  <span class="chip">第 {{ asset.page }} 页</span>
                  <span v-if="asset.section" class="chip">{{ asset.section }}</span>
                </div>
                <p class="asset-caption">{{ asset.caption || '（未识别到表注）' }}</p>
                <p v-if="asset.content_text" class="asset-content">{{ asset.content_text }}</p>
              </figcaption>
            </figure>
          </div>
        </div>
      </section>

      <!-- ----------------------------------------------------- pyramid -->
      <section v-else-if="tab === 'pyramid'" class="col">
        <div class="glass card">
          <div class="card-head">
            <div>
              <div class="card-title">C 层 · 摘要</div>
              <div class="card-subtitle">直接取自论文 abstract，未经模型改写；是检索的入口层</div>
            </div>
            <button class="btn btn-plain btn-sm" @click="router.push('/qa')">基于此提问</button>
          </div>
          <p v-if="detail.pyramid.C" class="abstract">{{ detail.pyramid.C.text }}</p>
          <EmptyState v-else title="尚无 C 层单元" />
          <div v-if="detail.pyramid.C?.cross_links?.length" style="margin-top: 12px">
            <div class="card-subtitle">跨论文关联</div>
            <div class="chip-row" style="margin-top: 6px">
              <button
                v-for="link in detail.pyramid.C.cross_links"
                :key="link.hash"
                class="chip chip-accent clickable"
                @click="router.push(`/paper/${link.hash}`)"
              >
                {{ link.title || link.hash.slice(0, 8) }} · {{ (link.score * 100).toFixed(0) }}%
              </button>
            </div>
          </div>
        </div>

        <div class="glass card">
          <div class="card-head">
            <div class="card-title">B 层 · 维度要点（{{ detail.pyramid.B.length }}）</div>
            <div class="card-subtitle">由 A 层再精简而来，是知识图谱的语料来源</div>
          </div>
          <EmptyState v-if="detail.pyramid.B.length === 0" title="尚无 B 层单元" />
          <div v-else class="layer-list">
            <article v-for="unit in detail.pyramid.B" :key="unit.id" class="layer-unit">
              <div class="layer-head">
                <span class="chip chip-accent">{{ unit.title || unit.section }}</span>
                <span class="chip mono">{{ unit.id }}</span>
                <span class="faint">{{ unit.text.length }} 字</span>
              </div>
              <p>{{ unit.text }}</p>
              <ul v-if="pointsOf(unit).length" class="point-list">
                <li v-for="(point, index) in pointsOf(unit)" :key="index">
                  {{ point.text }}
                  <span v-for="label in point.assets" :key="label" class="chip">{{ label }}</span>
                </li>
              </ul>
              <div class="layer-links">
                <span class="faint">上层：</span>
                <span v-for="parent in unit.parents ?? []" :key="parent" class="chip mono">{{ parent }}</span>
                <span class="faint">下层：</span>
                <span v-for="child in unit.children ?? []" :key="child" class="chip mono">{{ child }}</span>
              </div>
            </article>
          </div>
        </div>

        <div class="glass card">
          <div class="card-head">
            <div class="card-title">A 层 · 章节详细大意（{{ detail.pyramid.A.length }}）</div>
            <div class="card-subtitle">由原文精简得到，是回答细节问题时的证据来源</div>
          </div>
          <EmptyState v-if="detail.pyramid.A.length === 0" title="尚无 A 层单元" />
          <div v-else class="layer-list">
            <article v-for="unit in detail.pyramid.A" :key="unit.id" class="layer-unit">
              <div class="layer-head">
                <span class="chip">{{ unit.title || '章节' }}</span>
                <span class="chip mono">{{ unit.id }}</span>
                <span class="faint">{{ unit.text.length }} 字</span>
              </div>
              <p>{{ unit.text }}</p>
            </article>
          </div>
        </div>
      </section>

      <!-- ---------------------------------------------------- fishbone -->
      <section v-else class="col">
        <div v-if="!fishbone" class="glass card dim">正在加载鱼骨图谱</div>
        <template v-else>
          <div class="glass card">
            <div class="card-head">
              <div>
                <div class="card-title">七维鱼骨</div>
                <div class="card-subtitle">
                  节点 {{ fishbone.counts.nodes }}，挂载图表 {{ fishbone.counts.assets }}，覆盖维度
                  {{ fishbone.counts.sections }}/7
                </div>
              </div>
              <div class="btn-row">
                <button class="btn btn-plain btn-sm" @click="router.push('/graph')">打开图谱页</button>
                <a class="btn btn-plain btn-sm" :href="kgApi.exportUrl({ hashes: [hash] })" target="_blank">
                  导出 JSON
                </a>
              </div>
            </div>
          </div>

          <div v-for="section in fishbone.spine" :key="section.slug" class="glass card">
            <div class="card-head">
              <div class="card-title">
                {{ section.order }}. {{ section.label }}
                <span class="chip">{{ section.nodes.length }} 个节点</span>
              </div>
              <span class="card-subtitle mono">{{ section.slug }}</span>
            </div>
            <EmptyState v-if="section.nodes.length === 0" title="该维度没有抽取到内容" />
            <ul v-else class="spine-nodes">
              <li v-for="node in section.nodes" :key="node.key" class="spine-node">
                <div class="spine-node-head">
                  <span v-if="node.meta?.importance" class="faint">
                    重要度 {{ (Number(node.meta.importance) * 100).toFixed(0) }}%
                  </span>
                </div>
                <p class="spine-detail">{{ node.detail }}</p>
                <div v-if="node.assets.length" class="asset-links">
                  <button
                    v-for="asset in node.assets"
                    :key="asset.key"
                    class="asset-link"
                    @click="openAsset(asset.label, asset.caption)"
                  >
                    <img :src="papersApi.assetUrl(hash, asset.label)" :alt="asset.label" />
                    <span class="chip chip-accent">{{ asset.label }}</span>
                    <span class="faint truncate">{{ asset.caption || asset.kind }}</span>
                  </button>
                </div>
              </li>
            </ul>
          </div>
        </template>
      </section>
    </template>

    <div v-if="zoomed" class="lightbox" @click.self="zoomed = null">
      <div class="lightbox-body glass glass-strong">
        <div class="row-between">
          <div class="card-title">{{ zoomed.label }}</div>
          <button class="btn btn-plain btn-sm" @click="zoomed = null">关闭</button>
        </div>
        <img :src="papersApi.assetUrl(hash, zoomed.label)" :alt="zoomed.label" />
        <p class="asset-caption">{{ zoomed.caption }}</p>
      </div>
    </div>
  </div>
</template>

<script lang="ts">
export default { name: 'PaperView' }
</script>

<style scoped>
.paper-title {
  margin: 0;
  font-size: 18px;
  line-height: 1.45;
}

.paper-meta {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: 8px;
}

.tabs {
  display: flex;
  gap: 2px;
  padding: 6px;
  flex-wrap: wrap;
}

.tab {
  padding: 7px 14px;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-dim);
  font-family: inherit;
  font-size: 13px;
  cursor: pointer;
  transition: background var(--transition), color var(--transition);
}

.tab:hover { background: var(--glass); color: var(--text); }

.tab.active {
  background: var(--accent-soft);
  color: var(--accent-strong);
  font-weight: 550;
}

.abstract {
  font-size: 13.5px;
  line-height: 1.8;
  color: var(--text);
}

.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
  gap: 10px;
}

.stat {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 10px 12px;
  border: 1px solid var(--glass-border);
  border-radius: var(--radius-sm);
  background: var(--glass);
  font-size: 12px;
}

.stat b { font-size: 18px; font-weight: 600; }

.pdf-frame {
  width: 100%;
  height: 74vh;
  border: 1px solid var(--glass-border);
  border-radius: var(--radius-md);
  background: var(--glass);
}

.okf-layout {
  display: grid;
  grid-template-columns: 280px minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}

@media (max-width: 980px) {
  .okf-layout { grid-template-columns: 1fr; }
}

.concept-list {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 62vh;
  overflow-y: auto;
}

.concept-item { border-bottom: 1px solid var(--glass-border); }

.concept-link {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 7px 4px;
  border: none;
  background: none;
  color: var(--text);
  font: inherit;
  font-size: 12.5px;
  text-align: left;
  cursor: pointer;
}

.concept-link:hover { color: var(--accent-strong); }

.concept-type {
  flex: 0 0 auto;
  font-size: 11px;
  color: var(--text-faint);
}

.frontmatter {
  margin-bottom: 14px;
  font-size: 12px;
}

.frontmatter summary {
  cursor: pointer;
  color: var(--text-faint);
  margin-bottom: 6px;
}

.asset-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 14px;
}

.asset {
  margin: 0;
  border: 1px solid var(--glass-border);
  border-radius: var(--radius-md);
  background: var(--glass);
  overflow: hidden;
}

.asset img {
  width: 100%;
  max-height: 260px;
  object-fit: contain;
  background: rgba(0, 0, 0, 0.16);
  cursor: zoom-in;
  display: block;
}

.asset figcaption { padding: 10px 12px; }

.asset-head {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}

.asset-caption {
  margin: 0;
  font-size: 12.5px;
  color: var(--text-dim);
  line-height: 1.65;
}

.asset-content {
  margin: 8px 0 0;
  font-size: 12px;
  color: var(--text-faint);
  white-space: pre-wrap;
}

.layer-list { display: flex; flex-direction: column; gap: 12px; }

.layer-unit {
  padding: 11px 13px;
  border: 1px solid var(--glass-border);
  border-radius: var(--radius-sm);
  background: var(--glass);
}

.layer-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}

.layer-unit p { margin: 0; font-size: 13px; line-height: 1.75; }

.point-list {
  margin: 10px 0 0;
  padding-left: 20px;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--text-dim);
}

.point-list .chip { margin-left: 6px; }

.layer-links {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  align-items: center;
  margin-top: 10px;
  font-size: 11.5px;
}

.spine-nodes { list-style: none; margin: 0; padding: 0; }

.spine-node {
  padding: 11px 0;
  border-bottom: 1px solid var(--glass-border);
}

.spine-node:last-child { border-bottom: none; }

.spine-node-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13.5px;
  font-weight: 550;
}

.spine-detail {
  margin: 6px 0 0;
  font-size: 12.5px;
  color: var(--text-dim);
  line-height: 1.7;
}

.asset-links {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 10px;
}

.asset-link {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  border: 1px solid var(--glass-border);
  border-radius: var(--radius-sm);
  background: var(--glass);
  color: var(--text);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  max-width: 260px;
}

.asset-link img {
  width: 54px;
  height: 40px;
  object-fit: cover;
  border-radius: 5px;
}

.clickable { cursor: pointer; }

.lightbox {
  position: fixed;
  inset: 0;
  z-index: 70;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 30px;
  background: rgba(0, 0, 0, 0.55);
  backdrop-filter: blur(6px);
}

.lightbox-body {
  max-width: min(1000px, 92vw);
  max-height: 90vh;
  overflow-y: auto;
  padding: 16px;
}

.lightbox-body img {
  width: 100%;
  border-radius: var(--radius-sm);
  margin-top: 10px;
}
</style>

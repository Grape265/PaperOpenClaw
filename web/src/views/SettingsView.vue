<script setup lang="ts">
/**
 * Settings page (FR-14).
 *
 * API keys are shown masked and echoed back unchanged: the backend recognises a
 * masked value and keeps the stored secret, so saving never destroys a key the
 * user did not retype.
 */
import { computed, onMounted, ref } from 'vue'
import { systemApi } from '../api/system'
import type { DoctorReport, HealthReport, ProviderConfig, ProviderPreset, Settings } from '../api/types'
import EmptyState from '../components/EmptyState.vue'
import { uiStore } from '../stores/ui'

const settings = ref<Settings | null>(null)
const presets = ref<ProviderPreset[]>([])
const health = ref<HealthReport | null>(null)
const doctor = ref<DoctorReport | null>(null)
const loading = ref(true)
const saving = ref(false)
const testing = ref('')
const rebuildHint = ref(false)

const KINDS = [
  { key: 'parse', label: '解析模型', hint: '需要支持 PDF 或图像输入，用于入库解析' },
  { key: 'chat', label: '问答模型', hint: '用于问答、头脑风暴与主持人汇总' },
  { key: 'digest', label: '摘要模型', hint: '用于 A/B 层精简；未配置时回退到问答模型' },
]

const modelOptions = computed(() => {
  const list = new Set<string>()
  for (const provider of settings.value?.providers ?? []) {
    for (const model of provider.models ?? []) list.add(`${provider.id}::${model}`)
  }
  return Array.from(list).sort()
})

async function load(): Promise<void> {
  loading.value = true
  try {
    const [loaded, presetData, healthData] = await Promise.all([
      systemApi.getSettings(),
      systemApi.providerPresets(),
      systemApi.health().catch(() => null),
    ])
    settings.value = loaded
    presets.value = presetData.items
    health.value = healthData
  } catch (error) {
    uiStore.error(`加载设置失败：${(error as Error).message}`)
  } finally {
    loading.value = false
  }
}

async function save(): Promise<void> {
  if (!settings.value) return
  saving.value = true
  try {
    const payload = JSON.parse(JSON.stringify(settings.value)) as Record<string, unknown>
    const result = await systemApi.updateSettings(payload)
    settings.value = result.settings
    rebuildHint.value = result.embedding_changed
    uiStore.ok(result.embedding_changed ? '设置已保存；嵌入配置变化，建议重建向量库' : '设置已保存')
    health.value = await systemApi.health().catch(() => health.value)
  } catch (error) {
    uiStore.error(`保存失败：${(error as Error).message}`)
  } finally {
    saving.value = false
  }
}

function addPreset(preset: ProviderPreset): void {
  if (!settings.value) return
  if (settings.value.providers.some((item) => item.id === preset.id)) {
    uiStore.warn(`提供方 ${preset.name} 已存在`)
    return
  }
  settings.value.providers.push({
    id: preset.id,
    name: preset.name,
    base_url: preset.base_url,
    api_key: '',
    models: [...preset.models],
    vision: preset.vision,
    timeout: 600,
    max_retries: 2,
    enabled: true,
    api_key_set: false,
  })
}

function addBlank(): void {
  if (!settings.value) return
  settings.value.providers.push({
    id: `custom-${settings.value.providers.length + 1}`,
    name: '自定义提供方',
    base_url: 'http://127.0.0.1:8000/v1',
    api_key: '',
    models: [],
    vision: false,
    timeout: 600,
    max_retries: 2,
    enabled: true,
    api_key_set: false,
  })
}

function removeProvider(index: number): void {
  if (!settings.value) return
  const provider = settings.value.providers[index]
  if (!window.confirm(`确认移除提供方「${provider.name || provider.id}」？`)) return
  settings.value.providers.splice(index, 1)
}

function editModels(provider: ProviderConfig): void {
  const current = provider.models.join(', ')
  const next = window.prompt('模型列表（以英文逗号分隔）', current)
  if (next === null) return
  provider.models = next
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
}

async function test(provider: ProviderConfig, kind: string): Promise<void> {
  testing.value = `${provider.id}-${kind}`
  try {
    const model = kind === 'embedding'
      ? settings.value?.embedding.remote_model || provider.models[0] || ''
      : kind === 'parse'
        ? settings.value?.routing.parse.model || provider.models[0] || ''
        : provider.models[0] || ''
    const result = await systemApi.testProvider({ provider: provider.id, model, kind })
    if (kind === 'parse') {
      uiStore.ok(`${provider.name}（${model}）支持图像解析，耗时 ${result.latency_ms}ms${result.sample ? `，返回：${result.sample}` : ''}`)
    } else {
      uiStore.ok(`${provider.name} 连通正常，耗时 ${result.latency_ms}ms${result.sample ? `，返回：${result.sample}` : ''}`)
    }
  } catch (error) {
    if (kind === 'parse') {
      uiStore.error(`该模型不能解析 PDF 图片：${(error as Error).message}`)
    } else {
      uiStore.error(`连通失败：${(error as Error).message}`)
    }
  } finally {
    testing.value = ''
  }
}

async function fetchModels(provider: ProviderConfig): Promise<void> {
  try {
    const data = await systemApi.listModels(provider.id)
    if (data.models.length === 0) {
      uiStore.warn('该提供方未返回模型列表')
      return
    }
    provider.models = data.models.slice(0, 60)
    uiStore.ok(`已获取 ${provider.models.length} 个模型`)
  } catch (error) {
    uiStore.error(`获取模型列表失败：${(error as Error).message}`)
  }
}

async function runDoctor(): Promise<void> {
  try {
    doctor.value = await systemApi.doctor()
  } catch (error) {
    uiStore.error(`诊断失败：${(error as Error).message}`)
  }
}

async function rebuild(): Promise<void> {
  const { ragApi } = await import('../api/rag')
  try {
    const data = await ragApi.rebuild({})
    uiStore.ok(`已提交向量重建任务：${data.job_id}`)
    rebuildHint.value = false
  } catch (error) {
    uiStore.error(`提交失败：${(error as Error).message}`)
  }
}

onMounted(load)
</script>

<template>
  <div v-if="loading" class="glass card dim">正在加载设置</div>

  <div v-else-if="settings" class="col">
    <section class="glass card">
      <div class="card-head">
        <div>
          <div class="card-title">运行状态</div>
          <div class="card-subtitle mono">{{ settings.data_dir }}</div>
        </div>
        <div class="btn-row">
          <button class="btn btn-ghost" @click="runDoctor">运行诊断</button>
          <button class="btn btn-primary" :disabled="saving" @click="save">
            {{ saving ? '保存中' : '保存设置' }}
          </button>
        </div>
      </div>

      <div class="stat-grid">
        <div class="stat"><span class="faint">版本</span><b>{{ health?.version ?? '—' }}</b></div>
        <div class="stat"><span class="faint">论文</span><b>{{ health?.counts.papers ?? 0 }}</b></div>
        <div class="stat"><span class="faint">语义单元</span><b>{{ health?.counts.rag_units ?? 0 }}</b></div>
        <div class="stat"><span class="faint">图谱节点</span><b>{{ health?.counts.kg_nodes ?? 0 }}</b></div>
        <div class="stat"><span class="faint">技能</span><b>{{ health?.counts.skills ?? 0 }}</b></div>
        <div class="stat">
          <span class="faint">嵌入方案</span>
          <b>{{ health?.embedding?.provider ?? '—' }}</b>
        </div>
      </div>

      <div v-if="rebuildHint" class="alert alert-warn" style="margin-top: 12px">
        嵌入配置已变更。已有向量与新模型不兼容，请重建向量库后再检索。
        <button class="btn btn-plain btn-sm" @click="rebuild">立即重建</button>
      </div>

      <div v-if="doctor" style="margin-top: 14px">
        <div class="card-subtitle">诊断结果</div>
        <table class="table">
          <thead><tr><th>检查项</th><th>结果</th><th>详情</th></tr></thead>
          <tbody>
            <tr v-for="check in doctor.checks" :key="check.name">
              <td>{{ check.name }}</td>
              <td><span class="chip" :class="check.ok ? 'chip-ok' : 'chip-danger'">{{ check.ok ? '通过' : '失败' }}</span></td>
              <td class="faint">{{ check.detail }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="glass card">
      <div class="card-head">
        <div>
          <div class="card-title">模型提供方</div>
          <div class="card-subtitle">
            全部走 OpenAI 兼容协议；API Key 只保存在本机配置文件中，界面仅回显掩码
          </div>
        </div>
        <div class="btn-row">
          <button class="btn btn-ghost" @click="addBlank">新增自定义</button>
        </div>
      </div>

      <div class="chip-row" style="margin-bottom: 14px">
        <button
          v-for="preset in presets"
          :key="preset.id"
          class="chip clickable"
          :class="{ 'chip-accent': settings.providers.some((item) => item.id === preset.id) }"
          @click="addPreset(preset)"
        >
          {{ preset.name }}
        </button>
      </div>

      <EmptyState
        v-if="settings.providers.length === 0"
        title="尚未配置提供方"
        hint="点击上方任一预设快速添加，然后填入 API Key。"
      />

      <div v-for="(provider, index) in settings.providers" :key="provider.id + index" class="provider">
        <div class="provider-head">
          <input v-model="provider.name" type="text" class="provider-name" placeholder="显示名称" />
          <input v-model="provider.id" type="text" class="provider-id" placeholder="标识" />
          <label class="checkbox">
            <input v-model="provider.vision" type="checkbox" />
            <span class="box" />
            支持视觉/PDF
          </label>
          <label class="checkbox">
            <input v-model="provider.enabled" type="checkbox" />
            <span class="box" />
            启用
          </label>
          <span class="grow" />
          <button class="btn btn-plain btn-sm" @click="removeProvider(index)">移除</button>
        </div>

        <div class="grid-2">
          <div class="field">
            <label class="field-label">Base URL</label>
            <input v-model="provider.base_url" type="text" placeholder="https://api.example.com/v1" />
          </div>
          <div class="field">
            <label class="field-label">
              API Key
              <span v-if="provider.api_key_set" class="chip chip-ok">已保存</span>
            </label>
            <input
              v-model="provider.api_key"
              type="password"
              :placeholder="provider.api_key_set ? '已保存，留空表示不修改' : 'sk-...'"
            />
            <span class="field-hint">显示为掩码时保存不会覆盖原有密钥</span>
          </div>
        </div>

        <div class="row-between">
          <div class="chip-row">
            <button class="chip clickable" @click="editModels(provider)">
              模型：{{ provider.models.length ? provider.models.join('、') : '未设置' }}
            </button>
          </div>
          <div class="btn-row">
            <button class="btn btn-plain btn-sm" @click="fetchModels(provider)">获取模型列表</button>
            <button
              class="btn btn-plain btn-sm"
              :disabled="testing === `${provider.id}-chat`"
              @click="test(provider, 'chat')"
            >
              测试对话
            </button>
            <button
              class="btn btn-plain btn-sm"
              :disabled="testing === `${provider.id}-parse`"
              @click="test(provider, 'parse')"
            >
              测试解析
            </button>
          </div>
        </div>
      </div>
    </section>

    <section class="glass card">
      <div class="card-head">
        <div>
          <div class="card-title">模型路由</div>
          <div class="card-subtitle">解析、问答与摘要可以使用不同模型</div>
        </div>
      </div>
      <div v-for="kind in KINDS" :key="kind.key" class="route-row">
        <div class="route-label">
          <div>{{ kind.label }}</div>
          <div class="field-hint">{{ kind.hint }}</div>
        </div>
        <select v-model="settings.routing[kind.key].provider">
          <option value="">未配置</option>
          <option v-for="provider in settings.providers" :key="provider.id" :value="provider.id">
            {{ provider.name || provider.id }}
          </option>
        </select>
        <input
          v-model="settings.routing[kind.key].model"
          type="text"
          list="model-options"
          placeholder="模型名称"
        />
      </div>
      <datalist id="model-options">
        <option v-for="option in modelOptions" :key="option" :value="option.split('::')[1]" />
      </datalist>
    </section>

    <section class="glass card">
      <div class="card-head">
        <div>
          <div class="card-title">嵌入方案</div>
          <div class="card-subtitle">
            local 使用本地模型（离线、不外传数据）；remote 走 /embeddings 接口；lexical 为纯词法降级方案
          </div>
        </div>
        <button class="btn btn-ghost" @click="rebuild">重建向量库</button>
      </div>

      <div class="grid-3">
        <div class="field">
          <label class="field-label">提供方</label>
          <select v-model="settings.embedding.provider">
            <option value="auto">自动（本地优先）</option>
            <option value="local">本地模型</option>
            <option value="remote">远程接口</option>
            <option value="lexical">离线词法</option>
          </select>
        </div>
        <div class="field">
          <label class="field-label">本地模型名</label>
          <input v-model="settings.embedding.model" type="text" placeholder="bge-small-zh-v1.5" />
        </div>
        <div class="field">
          <label class="field-label">本地模型路径</label>
          <input v-model="settings.embedding.model_path" type="text" placeholder="留空则自动查找" />
        </div>
      </div>

      <div class="grid-3">
        <div class="field">
          <label class="field-label">远程模型名</label>
          <input v-model="settings.embedding.remote_model" type="text" placeholder="text-embedding-v3" />
        </div>
        <div class="field">
          <label class="field-label">远程 Base URL</label>
          <input v-model="settings.embedding.base_url" type="text" placeholder="https://api.example.com/v1" />
        </div>
        <div class="field">
          <label class="field-label">远程 API Key</label>
          <input
            v-model="settings.embedding.api_key"
            type="password"
            :placeholder="settings.embedding.api_key_set ? '已保存，留空表示不修改' : ''"
          />
        </div>
      </div>

      <p class="field-hint">
        当前生效：{{ health?.embedding?.provider ?? '—' }}
        <span v-if="health?.embedding?.dimension">· 维度 {{ health.embedding.dimension }}</span>
        <span v-if="health?.embedding?.model">· 模型 {{ health.embedding.model }}</span>
      </p>
    </section>

    <section class="glass card">
      <div class="card-head">
        <div class="card-title">入库参数</div>
      </div>
      <div class="grid-3">
        <div class="field">
          <label class="field-label">PDF 送模方式</label>
          <select v-model="settings.ingest.pdf_mode">
            <option value="auto">自动（优先直传 PDF）</option>
            <option value="file">始终直传 PDF</option>
            <option value="image">逐页渲染为图像</option>
          </select>
        </div>
        <div class="field">
          <label class="field-label">每批页数</label>
          <input v-model.number="settings.ingest.vision_batch_pages" type="number" min="1" max="100" />
        </div>
        <div class="field">
          <label class="field-label">图表裁剪 DPI</label>
          <input v-model.number="settings.ingest.image_dpi" type="number" min="72" max="400" />
        </div>
      </div>
      <div class="grid-3">
        <div class="field">
          <label class="field-label">解析重试次数</label>
          <input v-model.number="settings.ingest.max_retry" type="number" min="0" max="5" />
        </div>
        <div class="field">
          <label class="field-label">最大页数</label>
          <input v-model.number="settings.ingest.max_pages" type="number" min="1" max="2000" />
        </div>
        <div class="field">
          <label class="field-label">图表最小面积占比</label>
          <input v-model.number="settings.ingest.min_asset_area_ratio" type="number" step="0.002" min="0" max="0.5" />
        </div>
      </div>
    </section>

    <section class="glass card">
      <div class="card-head">
        <div>
          <div class="card-title">检索参数</div>
          <div class="card-subtitle">控制 C→B→A 级联的召回数量与阈值</div>
        </div>
      </div>
      <div class="grid-3">
        <div class="field">
          <label class="field-label">C 层召回数</label>
          <input v-model.number="settings.retrieval.top_k_c" type="number" min="1" max="50" />
        </div>
        <div class="field">
          <label class="field-label">B 层召回数</label>
          <input v-model.number="settings.retrieval.top_k_b" type="number" min="1" max="50" />
        </div>
        <div class="field">
          <label class="field-label">A 层召回数</label>
          <input v-model.number="settings.retrieval.top_k_a" type="number" min="1" max="50" />
        </div>
      </div>
      <div class="grid-3">
        <div class="field">
          <label class="field-label">最低相似度阈值</label>
          <input v-model.number="settings.retrieval.min_score" type="number" step="0.01" min="0" max="1" />
          <span class="field-hint">低于该值时直接回答「库中无相关内容」</span>
        </div>
        <div class="field">
          <label class="field-label">C-C 关联阈值</label>
          <input v-model.number="settings.retrieval.cross_link_min_score" type="number" step="0.01" min="0" max="1" />
        </div>
        <div class="field">
          <label class="field-label">每篇 C-C 关联数</label>
          <input v-model.number="settings.retrieval.cross_link_top_k" type="number" min="1" max="20" />
        </div>
      </div>
      <label class="checkbox">
        <input v-model="settings.retrieval.stop_at_b" type="checkbox" />
        <span class="box" />
        B 层证据充分时不再下钻到 A 层（更快、更省 token）
      </label>
    </section>

    <section class="glass card">
      <div class="card-head">
        <div class="card-title">智能体参数</div>
      </div>
      <div class="grid-3">
        <div class="field">
          <label class="field-label">工具调用最大步数</label>
          <input v-model.number="settings.agent.max_steps" type="number" min="1" max="30" />
        </div>
        <div class="field">
          <label class="field-label">温度</label>
          <input v-model.number="settings.agent.temperature" type="number" step="0.1" min="0" max="2" />
        </div>
        <div class="field">
          <label class="field-label">单次最大 token</label>
          <input v-model.number="settings.agent.max_tokens" type="number" min="256" max="32000" />
        </div>
      </div>
      <div class="grid-3">
        <div class="field">
          <label class="field-label">头脑风暴轮数</label>
          <input v-model.number="settings.agent.brainstorm_rounds" type="number" min="1" max="6" />
        </div>
        <div class="field">
          <label class="field-label">最多角色数</label>
          <input v-model.number="settings.agent.brainstorm_max_agents" type="number" min="1" max="8" />
        </div>
        <div class="field">
          <label class="field-label">工具超时（秒）</label>
          <input v-model.number="settings.agent.tool_timeout" type="number" min="5" max="900" />
        </div>
      </div>
    </section>

    <section class="glass card">
      <div class="card-head">
        <div>
          <div class="card-title">服务</div>
          <div class="card-subtitle">修改监听地址与端口后需要重启服务生效</div>
        </div>
      </div>
      <div class="grid-3">
        <div class="field">
          <label class="field-label">监听地址</label>
          <input v-model="settings.server.host" type="text" />
        </div>
        <div class="field">
          <label class="field-label">端口</label>
          <input v-model.number="settings.server.port" type="number" min="1" max="65535" />
        </div>
        <div class="field">
          <label class="field-label">访问令牌</label>
          <input v-model="settings.server.auth_token" type="password" placeholder="留空表示不校验" />
          <span class="field-hint">设置后所有 API 请求需要携带该令牌</span>
        </div>
      </div>
    </section>

    <div class="glass card row-between">
      <span class="faint">修改完成后请点击保存；部分配置需要重启服务生效。</span>
      <button class="btn btn-primary" :disabled="saving" @click="save">
        {{ saving ? '保存中' : '保存设置' }}
      </button>
    </div>
  </div>
</template>

<style scoped>
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

.stat b { font-size: 17px; font-weight: 600; }

.provider {
  padding: 14px 0;
  border-bottom: 1px solid var(--glass-border);
}

.provider:last-child { border-bottom: none; }

.provider-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.provider-name { width: 190px; }
.provider-id { width: 150px; }

.route-row {
  display: grid;
  grid-template-columns: minmax(0, 1.3fr) minmax(0, 1fr) minmax(0, 1fr);
  gap: 12px;
  align-items: center;
  padding: 10px 0;
  border-bottom: 1px solid var(--glass-border);
}

.route-row:last-child { border-bottom: none; }

.route-label { font-size: 13px; }

@media (max-width: 820px) {
  .route-row { grid-template-columns: 1fr; }
}

.clickable { cursor: pointer; }
</style>

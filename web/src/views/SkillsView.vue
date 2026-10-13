<script setup lang="ts">
/**
 * Skills page (FR-9).
 *
 * Installation is deliberately explicit: a skill can inject instructions into an
 * agent's prompt and register tools that run scripts, so the page shows what a
 * skill declares before it is enabled.
 */
import { onMounted, ref } from 'vue'
import { skillsApi } from '../api/skills'
import { agentApi } from '../api/agent'
import type { Skill, SkillDetail, ToolInfo } from '../api/types'
import EmptyState from '../components/EmptyState.vue'
import MarkdownView from '../components/MarkdownView.vue'
import { uiStore } from '../stores/ui'

const skills = ref<Skill[]>([])
const tools = ref<ToolInfo[]>([])
const detail = ref<SkillDetail | null>(null)
const installPath = ref('')
const overwrite = ref(false)
const busy = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

async function load(): Promise<void> {
  try {
    skills.value = (await skillsApi.list()).items
    tools.value = (await agentApi.tools()).items
  } catch (error) {
    uiStore.error(`加载技能失败：${(error as Error).message}`)
  }
}

async function open(skill: Skill): Promise<void> {
  try {
    detail.value = await skillsApi.get(skill.id)
  } catch (error) {
    uiStore.error(`读取技能详情失败：${(error as Error).message}`)
  }
}

async function installFromPath(): Promise<void> {
  const path = installPath.value.trim()
  if (!path) {
    uiStore.warn('请输入技能目录或 .zip 的绝对路径')
    return
  }
  busy.value = true
  try {
    const skill = await skillsApi.installFromPath(path, true, overwrite.value)
    uiStore.ok(`已安装技能：${skill.name}`)
    installPath.value = ''
    await load()
  } catch (error) {
    uiStore.error(`安装失败：${(error as Error).message}`)
  } finally {
    busy.value = false
  }
}

async function uploadZip(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  busy.value = true
  try {
    const skill = await skillsApi.installFromZip(file, true, overwrite.value)
    uiStore.ok(`已安装技能：${skill.name}`)
    await load()
  } catch (error) {
    uiStore.error(`安装失败：${(error as Error).message}`)
  } finally {
    busy.value = false
  }
}

async function toggle(skill: Skill): Promise<void> {
  try {
    await skillsApi.setEnabled(skill.id, !skill.enabled)
    uiStore.ok(skill.enabled ? `已停用 ${skill.name}` : `已启用 ${skill.name}`)
    await load()
    if (detail.value?.skill.id === skill.id) await open(skill)
  } catch (error) {
    uiStore.error(`切换失败：${(error as Error).message}`)
  }
}

async function reload(skill: Skill): Promise<void> {
  try {
    await skillsApi.reload(skill.id)
    uiStore.ok('已重新读取 SKILL.md')
    await load()
    await open(skill)
  } catch (error) {
    uiStore.error(`重载失败：${(error as Error).message}`)
  }
}

async function remove(skill: Skill): Promise<void> {
  if (!window.confirm(`确认卸载技能「${skill.name}」？将删除其目录与登记。`)) return
  try {
    await skillsApi.remove(skill.id)
    if (detail.value?.skill.id === skill.id) detail.value = null
    uiStore.ok('已卸载技能')
    await load()
  } catch (error) {
    uiStore.error(`卸载失败：${(error as Error).message}`)
  }
}

onMounted(load)
</script>

<template>
  <div class="col">
    <section class="glass card">
      <div class="card-head">
        <div>
          <div class="card-title">技能（Skill）</div>
          <div class="card-subtitle">
            技能目录以 SKILL.md 为清单：正文作为指令注入智能体提示词，frontmatter 中声明的工具进入工具注册表
          </div>
        </div>
        <div class="btn-row">
          <button class="btn btn-ghost" @click="fileInput?.click()">上传 .zip 安装</button>
          <button class="btn btn-plain btn-sm" @click="load">刷新</button>
        </div>
      </div>

      <input ref="fileInput" type="file" accept=".zip" hidden @change="uploadZip" />

      <div class="install-row">
        <input
          v-model="installPath"
          type="text"
          class="grow"
          placeholder="技能目录或 .zip 的绝对路径，例如 D:\skills\pdf-tools"
          @keydown.enter="installFromPath"
        />
        <label class="checkbox">
          <input v-model="overwrite" type="checkbox" />
          <span class="box" />
          覆盖同名
        </label>
        <button class="btn btn-primary" :disabled="busy" @click="installFromPath">
          {{ busy ? '处理中' : '从路径安装' }}
        </button>
      </div>

      <p class="field-hint">
        SKILL.md 示例：frontmatter 需要 name / description，tools 中每项声明 name、description、type（script
        或 http）与 parameters；script 类工具通过参数文件与输出文件与主进程交换 JSON。
      </p>
    </section>

    <section class="glass card">
      <div class="card-head">
        <div class="card-title">已安装（{{ skills.length }}）</div>
      </div>

      <EmptyState
        v-if="skills.length === 0"
        title="尚未安装任何技能"
        hint="可以手工把技能目录放进 data/skills/ 下，刷新后即会被扫描到。"
      />

      <table v-else class="table">
        <thead>
          <tr>
            <th>名称</th>
            <th>标识</th>
            <th>版本</th>
            <th>工具</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="skill in skills" :key="skill.id">
            <td>
              <div>{{ skill.name }}</div>
              <div class="faint">{{ skill.description }}</div>
            </td>
            <td class="mono">{{ skill.id }}</td>
            <td>{{ skill.version || '—' }}</td>
            <td>
              <div class="chip-row">
                <span v-for="tool in skill.tools" :key="tool" class="chip">{{ tool }}</span>
                <span v-if="skill.tools.length === 0" class="faint">无</span>
              </div>
            </td>
            <td>
              <span class="chip" :class="skill.enabled ? 'chip-ok' : ''">
                {{ skill.enabled ? '已启用' : '已停用' }}
              </span>
            </td>
            <td>
              <button class="btn btn-plain btn-sm" @click="open(skill)">详情</button>
              <button class="btn btn-plain btn-sm" @click="toggle(skill)">
                {{ skill.enabled ? '停用' : '启用' }}
              </button>
              <button class="btn btn-plain btn-sm" @click="reload(skill)">重载</button>
              <button class="btn btn-plain btn-sm" @click="remove(skill)">卸载</button>
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <section v-if="detail" class="glass card">
      <div class="card-head">
        <div>
          <div class="card-title">{{ detail.skill.name }}</div>
          <div class="card-subtitle mono">{{ detail.skill.path }}</div>
        </div>
        <button class="btn btn-plain btn-sm" @click="detail = null">收起</button>
      </div>

      <div v-if="detail.warnings.length" class="alert alert-warn">
        <div v-for="(warning, index) in detail.warnings" :key="index">{{ warning }}</div>
      </div>

      <div class="detail-grid">
        <div>
          <div class="card-subtitle">指令正文</div>
          <MarkdownView :source="detail.instructions" />
        </div>
        <div>
          <div class="card-subtitle">声明的工具</div>
          <EmptyState v-if="detail.tool_declarations.length === 0" title="未声明工具" />
          <ul v-else class="decl-list">
            <li v-for="(tool, index) in detail.tool_declarations" :key="index">
              <b>{{ tool.name }}</b>
              <span class="chip">{{ tool.type || 'script' }}</span>
              <div class="faint">{{ tool.description }}</div>
              <pre v-if="tool.parameters">{{ JSON.stringify(tool.parameters, null, 2) }}</pre>
            </li>
          </ul>

          <div class="card-subtitle" style="margin-top: 14px">文件</div>
          <div class="chip-row">
            <span v-for="file in detail.files" :key="file" class="chip mono">{{ file }}</span>
          </div>
        </div>
      </div>
    </section>

    <section class="glass card">
      <div class="card-head">
        <div>
          <div class="card-title">工具注册表（{{ tools.length }}）</div>
          <div class="card-subtitle">智能体可调用的全部工具；每个智能体只会看到自己白名单中的项</div>
        </div>
      </div>
      <table class="table">
        <thead>
          <tr><th>工具</th><th>来源</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="tool in tools" :key="tool.name">
            <td class="mono">{{ tool.name }}</td>
            <td><span class="chip" :class="tool.source === 'builtin' ? '' : 'chip-accent'">{{ tool.source_name }}</span></td>
            <td>{{ tool.description }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>

<style scoped>
.install-row {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
}

.install-row input[type='text'] {
  min-width: 260px;
}

.detail-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr);
  gap: 20px;
}

@media (max-width: 900px) {
  .detail-grid { grid-template-columns: 1fr; }
}

.decl-list {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
  font-size: 12.5px;
}

.decl-list li {
  padding: 9px 0;
  border-bottom: 1px solid var(--glass-border);
}

.decl-list .chip { margin-left: 6px; }
</style>

<script setup lang="ts">
/**
 * 头脑风暴页（微信群聊天形式）。
 *
 * 各角色发言为左侧气泡（带头像 + 昵称，模仿群聊）；主持人黑板与轮次切换是居中的系统消息；
 * 最终汇总为居中的总结卡片。发起入口是顶部一条紧凑设置栏。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { agentApi } from '../api/agent'
import type { AgentProfile, SessionSummary } from '../api/types'
import MarkdownView from '../components/MarkdownView.vue'
import PaperSelector from '../components/PaperSelector.vue'
import { selectionStore } from '../stores/selection'
import { uiStore } from '../stores/ui'

type Msg =
  | { kind: 'agent'; agentId: string; name: string; round: number; text: string }
  | { kind: 'system'; round: number; text: string }
  | { kind: 'divider'; round: number; phase: string }
  | { kind: 'synthesis'; data: Record<string, any> }

const profiles = ref<AgentProfile[]>([])
const chosen = ref<string[]>(['methodologist', 'experimentalist', 'skeptic', 'connector'])
const topic = ref('')
const rounds = ref(1)
const showPicker = ref(false)
const running = ref(false)
const sessionId = ref('')
const msgs = ref<Msg[]>([])
const synthesis = ref<Record<string, any> | null>(null)
const sessions = ref<SessionSummary[]>([])
const showHistory = ref(false)
const awaitingUser = ref(false)
const steering = ref('')
const continuing = ref(false)
let closeStream: (() => void) | null = null

function avatarColor(id: string): string {
  let hash = 0
  for (const char of id) hash = (hash * 31 + char.charCodeAt(0)) >>> 0
  const hue = hash % 360
  return `hsl(${hue} 55% 45%)`
}

function avatarChar(name: string): string {
  return (name || '?').slice(0, 1)
}

const groupedChoices = computed(() =>
  profiles.value.filter((item) => item.id !== 'qa' && item.id !== 'moderator'),
)

function toggleAgent(id: string): void {
  const index = chosen.value.indexOf(id)
  if (index >= 0) {
    if (chosen.value.length > 1) chosen.value.splice(index, 1)
    else uiStore.warn('至少保留一个角色')
  } else if (chosen.value.length < 6) {
    chosen.value.push(id)
  } else {
    uiStore.warn('最多 6 个角色')
  }
}

function personaOf(id: string): string {
  return profiles.value.find((item) => item.id === id)?.persona ?? ''
}

async function start(): Promise<void> {
  const text = topic.value.trim()
  if (!text) {
    uiStore.warn('请填写议题')
    return
  }
  msgs.value = []
  synthesis.value = null
  running.value = true
  try {
    const data = await agentApi.brainstorm({
      topic: text,
      hashes: selectionStore.filter ?? [],
      agents: chosen.value.map((id) => ({ id })),
      rounds: rounds.value,
      moderator: { name: '主持人' },
    })
    sessionId.value = data.session_id
    watch(data.session_id)
    void loadSessions()
  } catch (error) {
    running.value = false
    uiStore.error(`发起失败：${(error as Error).message}`)
  }
}

function watch(id: string): void {
  closeStream?.()
  closeStream = agentApi.watchSession(id, {
    onRound: (data) => {
      const names: Record<string, string> = {
        decompose: '拆解议题',
        independent: '独立分析',
        revise: '基于黑板修正',
        synthesis: '主持汇总',
      }
      msgs.value.push({
        kind: 'divider',
        round: Number(data.round ?? 0),
        phase: names[String(data.phase)] ?? String(data.phase ?? ''),
      })
    },
    onPlan: (data) => {
      const sub = data.sub_questions as Record<string, string> | undefined
      if (sub) {
        const lines = Object.entries(sub).map(([role, q]) => `${role}：${q}`)
        msgs.value.push({ kind: 'system', round: 0, text: `主持人拆解议题\n${lines.join('\n')}` })
      }
    },
    onMessage: (data) => {
      msgs.value.push({
        kind: 'agent',
        agentId: String(data.agent_id ?? ''),
        name: String(data.agent_name ?? data.agent_id ?? ''),
        round: Number(data.round ?? 1),
        text: String(data.text ?? ''),
      })
    },
    onBlackboard: (data) => {
      msgs.value.push({
        kind: 'system',
        round: Number(data.round ?? 0),
        text: `第 ${data.round ?? ''} 轮黑板摘要\n${String(data.summary ?? '')}`,
      })
    },
    onSynthesis: (data) => {
      synthesis.value = data
      msgs.value.push({ kind: 'synthesis', data })
    },
    onAwaitingUser: (data) => {
      awaitingUser.value = true
      const round = Number(data.round ?? 0)
      const total = Number(data.total_rounds ?? 0)
      msgs.value.push({
        kind: 'system',
        round,
        text: `第 ${round} 轮已结束，共 ${total} 轮。你可以发表意见调整讨论方向，或选择「不发言」进入第 ${round + 1} 轮。`,
      })
    },
    onDone: () => {
      running.value = false
      awaitingUser.value = false
      closeStream?.()
      closeStream = null
      void loadSessions()
    },
    onError: (data) => {
      running.value = false
      uiStore.error(`头脑风暴失败：${data.message ?? ''}`)
    },
  })
}

function stop(): void {
  closeStream?.()
  closeStream = null
  running.value = false
  awaitingUser.value = false
}

async function continueRound(message?: string): Promise<void> {
  if (!sessionId.value) return
  continuing.value = true
  awaitingUser.value = false
  try {
    await agentApi.continueBrainstorm(sessionId.value, message)
    steering.value = ''
  } catch (error) {
    awaitingUser.value = true
    uiStore.error(`进入下一轮失败：${(error as Error).message}`)
  } finally {
    continuing.value = false
  }
}

function sendSteering(): void {
  void continueRound(steering.value.trim() || undefined)
}

function skipRound(): void {
  void continueRound(undefined)
}

async function loadProfiles(): Promise<void> {
  try {
    profiles.value = (await agentApi.profiles()).items
  } catch (error) {
    uiStore.error(`加载角色失败：${(error as Error).message}`)
  }
}

async function loadSessions(): Promise<void> {
  try {
    sessions.value = (await agentApi.sessions({ kind: 'brainstorm', page_size: 20 })).items
  } catch {
    /* 历史可选 */
  }
}

async function openSession(id: string): Promise<void> {
  stop()
  try {
    const detail = await agentApi.session(id)
    sessionId.value = id
    msgs.value = []
    synthesis.value = detail.synthesis
    const agents = new Map(detail.agents.map((agent) => [agent.id, agent.name]))
    // 按原始顺序重放：轮次 → 黑板 → 各角色发言 → 总结。
    const roundsSeen = new Set<number>()
    for (const message of detail.messages) {
      if (message.round && !roundsSeen.has(message.round) && message.role === 'assistant') {
        roundsSeen.add(message.round)
        msgs.value.push({ kind: 'divider', round: message.round, phase: message.round === 1 ? '独立分析' : '基于黑板修正' })
      }
      if (message.role === 'blackboard') {
        msgs.value.push({ kind: 'system', round: message.round, text: `第 ${message.round} 轮黑板摘要\n${message.content}` })
      } else if (message.role === 'synthesis') {
        try {
          synthesis.value = JSON.parse(message.content)
        } catch {
          synthesis.value = { synthesis: message.content }
        }
        msgs.value.push({ kind: 'synthesis', data: synthesis.value ?? {} })
      } else if (message.role === 'assistant' && message.agent_id) {
        msgs.value.push({
          kind: 'agent',
          agentId: message.agent_id,
          name: agents.get(message.agent_id) ?? message.agent_id,
          round: message.round,
          text: message.content,
        })
      }
    }
    showHistory.value = false
  } catch (error) {
    uiStore.error(`加载会话失败：${(error as Error).message}`)
  }
}

async function removeSession(id: string): Promise<void> {
  if (!window.confirm('确认删除该会话？')) return
  try {
    await agentApi.deleteSession(id)
    await loadSessions()
  } catch (error) {
    uiStore.error(`删除失败：${(error as Error).message}`)
  }
}

onMounted(async () => {
  await loadProfiles()
  await loadSessions()
})
onUnmounted(stop)
</script>

<template>
  <div class="col">
    <section class="glass card">
      <div class="field">
        <label class="field-label">议题</label>
        <textarea
          v-model="topic"
          rows="2"
          placeholder="例如：Transformer 的自注意力在长序列场景下的可行性，以及与稀疏注意力的取舍"
        />
      </div>

      <div class="setup-row">
        <label class="checkbox">
          <input v-model="showPicker" type="checkbox" />
          <span class="box" />
          限定论文范围（已选 {{ selectionStore.count }} 篇）
        </label>
        <span class="grow" />
        <span class="field-label">轮数</span>
        <input v-model.number="rounds" type="number" min="1" max="5" class="rounds-input" />
        <button class="btn btn-ghost" @click="showHistory = !showHistory">
          {{ showHistory ? '收起历史' : '历史会话' }}
        </button>
        <button v-if="running" class="btn btn-ghost" @click="stop">停止接收</button>
        <button class="btn btn-primary" :disabled="running" @click="start">
          {{ running ? '讨论进行中' : '开始讨论' }}
        </button>
      </div>

      <div class="roles">
        <button
          v-for="profile in groupedChoices"
          :key="profile.id"
          class="role-chip"
          :class="{ selected: chosen.includes(profile.id) }"
          :title="personaOf(profile.id)"
          @click="toggleAgent(profile.id)"
        >
          {{ profile.name }}
        </button>
      </div>

      <div v-if="showPicker" class="picker">
        <PaperSelector :page-size="30" />
      </div>
    </section>

    <section v-if="showHistory" class="glass card">
      <div class="card-head">
        <div class="card-title">历史会话</div>
        <button class="btn btn-plain btn-sm" @click="loadSessions">刷新</button>
      </div>
      <div v-if="sessions.length === 0" class="empty">暂无头脑风暴记录</div>
      <div v-else class="history-list">
        <div v-for="item in sessions" :key="item.id" class="history-item">
          <button class="history-main" @click="openSession(item.id)">
            <span class="grow truncate">{{ item.title || item.topic }}</span>
            <span class="faint">{{ item.status }} · {{ item.rounds }} 轮 · {{ item.message_count ?? 0 }} 条</span>
          </button>
          <button class="btn btn-plain btn-sm" @click="removeSession(item.id)">删除</button>
        </div>
      </div>
    </section>

    <section class="glass card group-chat">
      <div class="chat-head">
        <span class="card-title">讨论群</span>
        <span class="faint">会话 {{ sessionId || '（未开始）' }}</span>
      </div>

      <div class="chat-body">
        <div v-if="msgs.length === 0" class="empty">
          填写议题并选择角色，点击「开始讨论」。
          <p class="field-hint">第一轮各角色独立分析，之后通过黑板摘要交换观点。</p>
        </div>

        <template v-for="(msg, index) in msgs" :key="index">
          <div v-if="msg.kind === 'divider'" class="divider-row">
            <span class="divider-text">第 {{ msg.round }} 轮 · {{ msg.phase }}</span>
          </div>

          <div v-else-if="msg.kind === 'system'" class="system-msg">
            <span class="system-avatar">主持人</span>
            <div class="system-bubble">
              <MarkdownView :source="msg.text" />
            </div>
          </div>

          <div v-else-if="msg.kind === 'agent'" class="agent-msg">
            <span class="avatar" :style="{ background: avatarColor(msg.agentId) }">
              {{ avatarChar(msg.name) }}
            </span>
            <div class="agent-body">
              <div class="agent-meta">
                <span class="agent-name">{{ msg.name }}</span>
                <span class="faint">第 {{ msg.round }} 轮</span>
              </div>
              <div class="agent-bubble">
                <MarkdownView :source="msg.text" />
              </div>
            </div>
          </div>

          <div v-else-if="msg.kind === 'synthesis'" class="system-msg">
            <span class="system-avatar">总结</span>
            <div class="system-bubble synthesis-bubble">
              <MarkdownView :source="String(msg.data.synthesis ?? '')" />
              <template v-if="msg.data.consensus?.length">
                <div class="divider" />
                <div class="syn-title">共识</div>
                <ul class="syn-list">
                  <li v-for="(item, i) in msg.data.consensus" :key="i">
                    {{ item.point }}
                    <span v-for="name in item.supporters ?? []" :key="name" class="chip">{{ name }}</span>
                  </li>
                </ul>
              </template>
              <template v-if="msg.data.disagreements?.length">
                <div class="divider" />
                <div class="syn-title">分歧</div>
                <ul class="syn-list">
                  <li v-for="(item, i) in msg.data.disagreements" :key="i">
                    <b>{{ item.topic }}</b>
                    <div v-for="position in item.positions ?? []" :key="position.role" class="dim">
                      {{ position.role }}：{{ position.stance }}
                    </div>
                  </li>
                </ul>
              </template>
              <template v-if="msg.data.open_questions?.length">
                <div class="divider" />
                <div class="syn-title">待研究问题</div>
                <ul class="syn-list">
                  <li v-for="(item, i) in msg.data.open_questions" :key="i">{{ item }}</li>
                </ul>
              </template>
            </div>
          </div>
        </template>
      </div>

      <div v-if="awaitingUser" class="steering-bar">
        <input
          v-model="steering"
          type="text"
          class="steering-input"
          placeholder="发表意见（可修改讨论方向），留空则等同「不发言」"
          @keydown.enter="sendSteering"
        />
        <button class="btn btn-ghost" :disabled="continuing" @click="sendSteering">
          {{ continuing ? '提交中' : '发言并继续' }}
        </button>
        <button class="btn btn-plain" :disabled="continuing" @click="skipRound">
          不发言，进入下一轮
        </button>
      </div>
    </section>
  </div>
</template>

<style scoped>
.setup-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.rounds-input { width: 60px; }

.roles { display: flex; flex-wrap: wrap; gap: 8px; }

.role-chip {
  padding: 6px 14px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--bg);
  color: var(--text-dim);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}

.role-chip:hover { border-color: var(--accent); color: var(--text); }

.role-chip.selected {
  background: var(--accent-soft);
  border-color: var(--accent);
  color: var(--accent-strong);
  font-weight: 550;
}

.picker {
  margin-top: 12px;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  max-height: 300px;
  overflow-y: auto;
}

.history-list { display: flex; flex-direction: column; gap: 4px; }

.history-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

.history-main {
  display: flex;
  align-items: center;
  gap: 12px;
  flex: 1 1 auto;
  min-width: 0;
  border: none;
  background: none;
  color: var(--text);
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.history-main:hover { color: var(--accent-strong); }

.group-chat { padding-bottom: 14px; }

.chat-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 14px;
}

.chat-body {
  display: flex;
  flex-direction: column;
  gap: 14px;
  max-height: 62vh;
  overflow-y: auto;
  padding: 4px 6px;
}

.divider-row { text-align: center; }

.divider-text {
  display: inline-block;
  padding: 3px 12px;
  border-radius: 999px;
  background: var(--bg-hover);
  color: var(--text-faint);
  font-size: 12px;
}

.system-msg {
  display: flex;
  justify-content: center;
  flex-wrap: wrap;
  gap: 8px;
}

.system-avatar {
  align-self: center;
  padding: 3px 12px;
  border-radius: 999px;
  background: var(--bg-hover);
  color: var(--text-faint);
  font-size: 12px;
}

.system-bubble {
  max-width: 80%;
  padding: 10px 14px;
  border: 1px dashed var(--border-strong);
  border-radius: 10px;
  background: var(--bg);
  font-size: 13px;
  color: var(--text-dim);
}

.synthesis-bubble {
  border-style: solid;
  border-color: var(--accent-soft);
  color: var(--text);
}

.agent-msg { display: flex; gap: 10px; }

.avatar {
  flex: 0 0 38px;
  width: 38px;
  height: 38px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 16px;
  font-weight: 600;
  user-select: none;
}

.agent-body { min-width: 0; max-width: 78%; }

.agent-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 4px;
}

.agent-name { font-size: 12.5px; color: var(--text-faint); }

.agent-bubble {
  padding: 10px 14px;
  border: 1px solid var(--border);
  border-radius: 10px;
  border-top-left-radius: 2px;
  background: var(--bg);
  font-size: 13.5px;
  line-height: 1.75;
}

.syn-title { font-size: 13px; font-weight: 600; margin-bottom: 6px; }

.syn-list {
  margin: 0;
  padding-left: 20px;
  font-size: 13px;
  line-height: 1.75;
}

.syn-list .chip { margin-left: 6px; }

.steering-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  padding: 8px;
  border: 1px solid var(--accent-soft);
  border-radius: var(--radius-md);
  background: var(--bg);
}

.steering-input {
  flex: 1 1 auto;
  min-width: 0;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--text);
  font: inherit;
  font-size: 13px;
}
</style>

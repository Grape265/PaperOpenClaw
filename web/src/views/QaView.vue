<script setup lang="ts">
/**
 * 问答页（仿 DeepSeek 网页版）。
 *
 * - 左侧会话列表 + 「新对话」；
 * - 中间为沉浸式聊天区：提问在右、回答在左，流式输出；
 * - 不展示来源引用。
 */
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { agentApi } from '../api/agent'
import type { SessionSummary } from '../api/types'
import MarkdownView from '../components/MarkdownView.vue'
import PaperSelector from '../components/PaperSelector.vue'
import { selectionStore } from '../stores/selection'
import { uiStore } from '../stores/ui'

interface Turn {
  role: 'user' | 'assistant'
  text: string
  streaming: boolean
  error: string
}

const turns = ref<Turn[]>([])
const question = ref('')
const busy = ref(false)
const sessionId = ref<string | undefined>(undefined)
const sessions = ref<SessionSummary[]>([])
const showPicker = ref(false)
const railCollapsed = ref(false)
const scroller = ref<HTMLElement | null>(null)
let closeStream: (() => void) | null = null

const scope = computed(() => selectionStore.filter)

async function scrollToEnd(): Promise<void> {
  await nextTick()
  if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight
}

function newChat(): void {
  stop()
  turns.value = []
  sessionId.value = undefined
  question.value = ''
}

function ask(): void {
  const text = question.value.trim()
  if (!text || busy.value) return

  turns.value.push({ role: 'user', text, streaming: false, error: '' })
  const answer: Turn = { role: 'assistant', text: '', streaming: true, error: '' }
  turns.value.push(answer)
  question.value = ''
  busy.value = true
  void scrollToEnd()

  closeStream = agentApi.qaStream(
    { question: text, hashes: scope.value ?? undefined, session_id: sessionId.value },
    {
      onStatus: (data) => {
        if (answer.text === '') answer.text = String(data.message ?? '')
      },
      onCitations: () => {
        answer.text = '' // 检索完成后用流式正文覆盖占位状态
      },
      onDelta: (chunk) => {
        answer.text += chunk
        void scrollToEnd()
      },
      onDone: (data) => {
        answer.streaming = false
        busy.value = false
        if (data.session_id) {
          sessionId.value = String(data.session_id)
          void loadSessions()
        }
        if (!answer.text) answer.text = '（未返回内容）'
        void scrollToEnd()
      },
      onError: (data) => {
        answer.streaming = false
        answer.error = String(data.message ?? '请求失败')
        busy.value = false
        uiStore.error(answer.error)
      },
    },
  )
}

function stop(): void {
  closeStream?.()
  closeStream = null
  busy.value = false
  const last = turns.value[turns.value.length - 1]
  if (last?.streaming) last.streaming = false
}

async function loadSessions(): Promise<void> {
  try {
    sessions.value = (await agentApi.sessions({ kind: 'qa', page_size: 50 })).items
  } catch {
    /* 会话历史为可选功能 */
  }
}

async function openSession(id: string): Promise<void> {
  stop()
  try {
    const detail = await agentApi.session(id)
    sessionId.value = id
    turns.value = detail.messages
      .filter((message) => message.role === 'user' || message.role === 'assistant')
      .map((message) => ({
        role: message.role as 'user' | 'assistant',
        text: message.content,
        streaming: false,
        error: '',
      }))
    void scrollToEnd()
  } catch (error) {
    uiStore.error(`加载会话失败：${(error as Error).message}`)
  }
}

async function removeSession(id: string): Promise<void> {
  if (!window.confirm('确认删除该对话？')) return
  try {
    await agentApi.deleteSession(id)
    if (sessionId.value === id) newChat()
    await loadSessions()
  } catch (error) {
    uiStore.error(`删除失败：${(error as Error).message}`)
  }
}

const suggestions = [
  '这篇论文的方法解决了什么问题？',
  '实验使用了哪些数据集与指标？',
  '论文自述的局限性是什么？',
  '作者的核心贡献有哪些？',
]

onMounted(loadSessions)
onUnmounted(stop)
</script>

<template>
  <div class="qa-page" :class="{ collapsed: railCollapsed }">
    <aside class="rail">
      <button class="btn btn-primary new-chat" @click="newChat">新对话</button>
      <div class="rail-title">历史对话</div>
      <div class="conv-list">
        <div v-if="sessions.length === 0" class="faint" style="padding: 10px 6px">
          暂无历史对话
        </div>
        <button
          v-for="item in sessions"
          :key="item.id"
          class="conv-item"
          :class="{ active: sessionId === item.id }"
          @click="openSession(item.id)"
        >
          <span class="conv-text">{{ item.title || item.topic || '未命名对话' }}</span>
          <span class="conv-time">{{ item.updated_at.slice(5, 16) }}</span>
          <span class="conv-del" @click.stop="removeSession(item.id)">删除</span>
        </button>
      </div>
    </aside>

    <section class="chat">
      <header class="chat-head">
        <button class="btn btn-plain btn-sm rail-toggle" @click="railCollapsed = !railCollapsed">
          {{ railCollapsed ? '展开对话列表' : '收起对话列表' }}
        </button>
        <div class="chat-title">
          {{ sessions.find((item) => item.id === sessionId)?.title || '新对话' }}
        </div>
        <div class="btn-row">
          <span class="faint">范围：{{ selectionStore.count ? `${selectionStore.count} 篇` : '全部论文' }}</span>
          <button class="btn btn-ghost btn-sm" @click="showPicker = !showPicker">
            {{ showPicker ? '收起论文选择' : '选择论文' }}
          </button>
        </div>
      </header>

      <div v-if="showPicker" class="picker">
        <PaperSelector :page-size="30" />
      </div>

      <div ref="scroller" class="messages">
        <div v-if="turns.length === 0" class="welcome">
          <div class="welcome-mark">PaperOpenClaw</div>
          <p class="welcome-title">向你的论文库提问</p>
          <div class="suggestions">
            <button
              v-for="item in suggestions"
              :key="item"
              class="suggest"
              @click="question = item; ask()"
            >
              {{ item }}
            </button>
          </div>
        </div>

        <div v-for="(turn, index) in turns" :key="index" class="msg-row" :class="turn.role">
          <div class="bubble">
            <div v-if="turn.error" class="alert alert-error" style="margin: 0">{{ turn.error }}</div>
            <MarkdownView v-else-if="turn.text" :source="turn.text" />
            <div v-else-if="turn.streaming" class="thinking">
              <span class="spinner" /> 正在思考
            </div>
          </div>
        </div>
      </div>

      <div class="composer">
        <textarea
          v-model="question"
          rows="2"
          placeholder="输入问题，Ctrl+Enter 发送"
          @keydown.ctrl.enter.prevent="ask"
          @keydown.meta.enter.prevent="ask"
        />
        <div class="composer-bar">
          <span class="grow" />
          <button v-if="busy" class="btn btn-ghost btn-sm" @click="stop">停止</button>
          <button class="btn btn-primary btn-sm" :disabled="busy || !question.trim()" @click="ask">
            发送
          </button>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.qa-page {
  display: grid;
  grid-template-columns: 240px minmax(0, 1fr);
  gap: 16px;
  height: calc(100vh - 96px);
}

.qa-page.collapsed {
  grid-template-columns: minmax(0, 1fr);
}

.qa-page.collapsed .rail {
  display: none;
}

.rail-toggle {
  flex: 0 0 auto;
}

.rail {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg-elev);
  overflow-y: auto;
}

.new-chat { width: 100%; }

.rail-title { font-size: 12px; color: var(--text-faint); padding: 0 6px; }

.conv-list { display: flex; flex-direction: column; gap: 2px; }

.conv-item {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 2px;
  width: 100%;
  padding: 8px 10px;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text);
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.conv-item:hover { background: var(--bg-hover); }
.conv-item.active { background: var(--accent-soft); }

.conv-text { font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.conv-time { font-size: 11px; color: var(--text-faint); }

.conv-del {
  position: absolute;
  right: 8px;
  top: 50%;
  transform: translateY(-50%);
  font-size: 11px;
  color: var(--text-faint);
  opacity: 0;
}

.conv-item:hover .conv-del { opacity: 1; }
.conv-del:hover { color: var(--danger); }

.chat {
  display: flex;
  flex-direction: column;
  min-width: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg-elev);
  overflow: hidden;
}

.chat-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}

.chat-title { font-size: 14px; font-weight: 600; }

.picker {
  padding: 10px;
  border-bottom: 1px solid var(--border);
  max-height: 300px;
  overflow-y: auto;
}

.messages {
  flex: 1 1 auto;
  overflow-y: auto;
  padding: 20px 24px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.welcome { padding: 60px 20px; text-align: center; }

.welcome-mark {
  font-size: 26px;
  font-weight: 700;
  letter-spacing: 0.5px;
  margin-bottom: 6px;
}

.welcome-title { font-size: 15px; color: var(--text-dim); margin-bottom: 20px; }

.suggestions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  justify-content: center;
  max-width: 640px;
  margin: 0 auto;
}

.suggest {
  padding: 8px 14px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--bg);
  color: var(--text);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
  transition: border-color var(--transition), background var(--transition);
}

.suggest:hover { border-color: var(--accent); background: var(--bg-hover); }

.msg-row { display: flex; }

.msg-row.user { justify-content: flex-end; }
.msg-row.assistant { justify-content: flex-start; }

.bubble {
  max-width: 76%;
  padding: 12px 16px;
  border-radius: 14px;
  font-size: 14px;
  line-height: 1.8;
}

.msg-row.user .bubble {
  background: var(--accent);
  color: #fff;
  border-bottom-right-radius: 4px;
  white-space: pre-wrap;
}

.msg-row.assistant .bubble {
  background: var(--bg);
  border: 1px solid var(--border);
  border-bottom-left-radius: 4px;
}

.thinking { color: var(--text-dim); }

.composer {
  padding: 12px 16px;
  border-top: 1px solid var(--border);
}

.composer textarea {
  min-height: 46px;
  background: var(--bg);
}

.composer-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
}

@media (max-width: 860px) {
  .qa-page { grid-template-columns: 1fr; }
  .rail { display: none; }
}
</style>

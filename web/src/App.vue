<script setup lang="ts">
/**
 * 应用外壳：顶部横向导航 + 全宽内容区。
 *
 * 侧边栏已移除。导航从左到右排列，状态与主题切换放在最右侧。
 */
import { onMounted, ref } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'
import { uiStore } from './stores/ui'
import { selectionStore } from './stores/selection'
import { systemApi } from './api/system'
import Toasts from './components/Toasts.vue'

const route = useRoute()
const nav = [
  { to: '/library', label: '论文库' },
  { to: '/ingest', label: '导入' },
  { to: '/qa', label: '问答' },
  { to: '/brainstorm', label: '头脑风暴' },
  { to: '/graph', label: '知识图谱' },
  { to: '/skills', label: '技能' },
  { to: '/settings', label: '设置' },
]

const version = ref('')
const healthy = ref<boolean | null>(null)
const counts = ref<Record<string, number>>({})

onMounted(async () => {
  try {
    const report = await systemApi.health()
    version.value = report.version
    counts.value = report.counts
    healthy.value = true
  } catch {
    healthy.value = false
  }
})

function statusText(): string {
  if (healthy.value === null) return '连接中'
  if (!healthy.value) return '后端未连接'
  return '已连接'
}
</script>

<template>
  <div class="app">
    <header class="topbar">
      <div class="brand">
        <span class="brand-name">PaperOpenClaw</span>
      </div>

      <nav class="nav">
        <RouterLink
          v-for="item in nav"
          :key="item.to"
          :to="item.to"
          class="nav-item"
          :class="{ active: route.path === item.to || route.path.startsWith(item.to + '/') }"
        >
          {{ item.label }}
        </RouterLink>
      </nav>

      <div class="top-actions">
        <span v-if="selectionStore.count" class="chip chip-accent">
          已选 {{ selectionStore.count }}
        </span>
        <span class="status" :class="{ bad: healthy === false }">
          {{ statusText() }} {{ counts.papers ?? 0 }} 篇
        </span>
        <button class="btn btn-plain btn-sm" @click="uiStore.toggleTheme()">
          {{ uiStore.state.theme === 'dark' ? '浅色' : '深色' }}
        </button>
      </div>
    </header>

    <main class="main">
      <RouterView />
    </main>
  </div>

  <Toasts />
</template>

<style scoped>
.app {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--bg);
}

.topbar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 20px;
  padding: 10px 20px;
  border-bottom: 1px solid var(--border);
  background: var(--bg);
  position: sticky;
  top: 0;
  z-index: 40;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  white-space: nowrap;
}

.brand-name {
  font-size: 15px;
  font-weight: 650;
  letter-spacing: 0.2px;
}

.brand-divider {
  width: 1px;
  height: 14px;
  background: var(--border-strong);
}

.brand-title {
  font-size: 13px;
  color: var(--text-dim);
}

.nav {
  display: flex;
  align-items: center;
  gap: 2px;
  flex: 1 1 auto;
  overflow-x: auto;
}

.nav-item {
  display: block;
  padding: 7px 13px;
  border-radius: var(--radius-sm);
  color: var(--text-dim);
  font-size: 13.5px;
  text-decoration: none;
  white-space: nowrap;
  transition: background var(--transition), color var(--transition);
}

.nav-item:hover { background: var(--bg-hover); color: var(--text); text-decoration: none; }

.nav-item.active {
  background: var(--accent-soft);
  color: var(--accent-strong);
  font-weight: 550;
}

.top-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  white-space: nowrap;
}

.status { font-size: 12px; color: var(--text-faint); }
.status.bad { color: var(--danger); }

.main {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 18px 22px 40px;
}

@media (max-width: 900px) {
  .topbar { flex-wrap: wrap; gap: 8px; }
  .nav { order: 3; width: 100%; }
  .brand-title { display: none; }
}
</style>

<script setup lang="ts">
import { uiStore } from '../stores/ui'
</script>

<template>
  <div class="toasts">
    <div
      v-for="toast in uiStore.state.toasts"
      :key="toast.id"
      class="toast glass glass-strong"
      :class="`toast-${toast.kind}`"
    >
      <span class="grow">{{ toast.message }}</span>
      <button class="btn btn-plain btn-sm" @click="uiStore.dismiss(toast.id)">关闭</button>
    </div>
  </div>
</template>

<style scoped>
.toasts {
  position: fixed;
  right: 18px;
  bottom: 18px;
  z-index: 60;
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-width: min(420px, 88vw);
}

.toast {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 12px;
  font-size: 13px;
  line-height: 1.55;
}

.toast-ok { border-color: rgba(53, 192, 127, 0.3); }
.toast-warn { border-color: rgba(240, 180, 41, 0.32); }
.toast-error { border-color: rgba(255, 107, 107, 0.34); }
</style>

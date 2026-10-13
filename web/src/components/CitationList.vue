<script setup lang="ts">
import type { Citation } from '../api/types'

defineProps<{ citations: Citation[]; compact?: boolean }>()

const emit = defineEmits<{ (event: 'open', citation: Citation): void }>()

function layerLabel(layer: string): string {
  if (layer === 'C') return 'C 层摘要'
  if (layer === 'B') return 'B 层要点'
  if (layer === 'A') return 'A 层大意'
  return `${layer} 层`
}
</script>

<template>
  <div v-if="citations.length" class="citations">
    <div class="citations-head">引用来源（{{ citations.length }}）</div>
    <ol class="citation-list">
      <li v-for="(item, index) in citations" :key="item.unit_id || index" class="citation">
        <div class="citation-top">
          <span class="chip chip-accent">[{{ index + 1 }}]</span>
          <span class="chip">{{ layerLabel(item.layer) }}</span>
          <span v-if="item.section_label" class="chip">{{ item.section_label }}</span>
          <span class="chip mono">{{ item.hash.slice(0, 8) }}</span>
          <span class="grow" />
          <span class="faint score">{{ (item.score * 100).toFixed(0) }}%</span>
        </div>
        <button class="citation-title" @click="emit('open', item)">{{ item.title || '未命名论文' }}</button>
        <p v-if="!compact && item.snippet" class="citation-snippet">{{ item.snippet }}</p>
        <div v-if="item.asset_refs?.length" class="chip-row">
          <span v-for="label in item.asset_refs" :key="label" class="chip">{{ label }}</span>
        </div>
      </li>
    </ol>
  </div>
</template>

<style scoped>
.citations {
  margin-top: 14px;
}

.citations-head {
  font-size: 12px;
  color: var(--text-dim);
  margin-bottom: 8px;
}

.citation-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.citation {
  padding: 9px 11px;
  border: 1px solid var(--glass-border);
  border-radius: var(--radius-sm);
  background: var(--glass);
}

.citation-top {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 4px;
}

.score {
  font-size: 11.5px;
}

.citation-title {
  display: block;
  padding: 0;
  border: none;
  background: none;
  color: var(--text);
  font: inherit;
  font-size: 13px;
  font-weight: 550;
  text-align: left;
  cursor: pointer;
}

.citation-title:hover {
  color: var(--accent-strong);
  text-decoration: underline;
}

.citation-snippet {
  margin: 5px 0 0;
  font-size: 12.5px;
  color: var(--text-dim);
  line-height: 1.65;
}

.citation .chip-row {
  margin-top: 6px;
}
</style>

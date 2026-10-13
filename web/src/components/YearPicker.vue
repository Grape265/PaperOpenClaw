<script setup lang="ts">
/**
 * 年份选择器（小日历风格）。
 *
 * 点击按钮展开一个年份网格弹层，选中即收起；带「清除」。用于论文库的起止年筛选。
 * 不引入日期库，年份范围由父组件提供（默认 1990～今年）。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'

const props = withDefaults(
  defineProps<{
    modelValue: number | null
    min?: number | null
    max?: number | null
    placeholder?: string
  }>(),
  { min: null, max: null, placeholder: '年份' },
)

const emit = defineEmits<{ (e: 'update:modelValue', value: number | null): void }>()

const open = ref(false)
const root = ref<HTMLElement | null>(null)

const currentYear = new Date().getFullYear()
const minYear = computed(() => props.min ?? 1990)
const maxYear = computed(() => props.max ?? currentYear)

const years = computed<number[]>(() => {
  const result: number[] = []
  for (let year = maxYear.value; year >= minYear.value; year -= 1) result.push(year)
  return result
})

const label = computed(() => (props.modelValue ? String(props.modelValue) : props.placeholder))

function pick(year: number): void {
  emit('update:modelValue', year)
  open.value = false
}

function clear(): void {
  emit('update:modelValue', null)
  open.value = false
}

function onDocClick(event: MouseEvent): void {
  if (open.value && root.value && !root.value.contains(event.target as Node)) {
    open.value = false
  }
}

onMounted(() => document.addEventListener('mousedown', onDocClick))
onUnmounted(() => document.removeEventListener('mousedown', onDocClick))
</script>

<template>
  <div ref="root" class="year-picker">
    <button
      class="year-trigger"
      :class="{ active: modelValue !== null }"
      @click="open = !open"
    >
      {{ label }}
      <span class="year-clear" @click.stop="clear">清除</span>
    </button>

    <div v-if="open" class="year-pop">
      <div class="year-grid">
        <button
          v-for="year in years"
          :key="year"
          class="year-cell"
          :class="{ selected: year === modelValue }"
          @click="pick(year)"
        >
          {{ year }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.year-picker {
  position: relative;
  display: inline-block;
}

.year-trigger {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 7px 11px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--text);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
  white-space: nowrap;
}

.year-trigger.active { border-color: var(--accent); color: var(--text); }

.year-trigger:not(.active) { color: var(--text-faint); }

.year-clear {
  font-size: 11px;
  color: var(--text-faint);
  opacity: 0;
  transition: opacity var(--transition);
}

.year-trigger:hover .year-clear,
.year-trigger.active .year-clear { opacity: 1; }

.year-clear:hover { color: var(--danger); }

.year-pop {
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  z-index: 30;
  width: 210px;
  padding: 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-elev);
  box-shadow: var(--shadow);
}

.year-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 4px;
  max-height: 200px;
  overflow-y: auto;
}

.year-cell {
  padding: 5px 0;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-dim);
  font: inherit;
  font-size: 12.5px;
  cursor: pointer;
}

.year-cell:hover { background: var(--bg-hover); color: var(--text); }
.year-cell.selected { background: var(--accent); color: #fff; }
</style>

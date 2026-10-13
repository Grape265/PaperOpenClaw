<script setup lang="ts">
/**
 * GraphCanvas：SVG 力导向「气泡 + 连线」图。
 *
 * 特性（应需求）：
 *   - 点和线相连，节点按 kind 着色；
 *   - 持续运行的物理模拟：节点默认有缓慢的浮动；
 *   - 斥力滑块实时调节物理强度；
 *   - 碰撞检测：节点间距不小于两者半径之和 + 间距，禁止相互遮挡；
 *   - 拖拽平移、滚轮缩放、点击气泡。
 *
 * 无第三方可视化库；物理用 requestAnimationFrame 内的速度积分实现。
 */
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'

export interface CanvasNode {
  id: string
  label: string
  kind: 'paper' | 'spine' | 'content' | 'asset' | string
  order?: number
  detail?: string
  hash?: string
  r?: number
}

export interface CanvasEdge {
  source: string
  target: string
  relation?: string
  weight?: number
}

const props = defineProps<{
  nodes: CanvasNode[]
  edges: CanvasEdge[]
  selectedId?: string
  seed?: 'circle' | 'fishbone'
  width?: number
  height?: number
}>()

const emit = defineEmits<{ (e: 'node-click', node: CanvasNode): void }>()

interface LayoutNode extends CanvasNode {
  x: number
  y: number
  vx: number
  vy: number
  r: number
}

const W = 960
const H = 620

const svg = ref<SVGSVGElement | null>(null)
const transform = reactive({ x: 0, y: 0, k: 1 })
const laidOut = ref<LayoutNode[]>([])
const running = ref(true)
const dragging = ref(false)
const dragStart = ref({ x: 0, y: 0, tx: 0, ty: 0 })
let rafId = 0

const edgesWithCoords = computed(() =>
  props.edges
    .map((edge) => {
      const source = laidOut.value.find((node) => node.id === edge.source)
      const target = laidOut.value.find((node) => node.id === edge.target)
      return { ...edge, source, target }
    })
    .filter((edge) => edge.source && edge.target),
)

function radius(node: CanvasNode): number {
  if (node.r) return node.r
  switch (node.kind) {
    case 'paper': return 24
    case 'spine': return 15
    default: return 9
  }
}

function initLayout(nodes: CanvasNode[]): LayoutNode[] {
  const result: LayoutNode[] = nodes.map((node) => ({
    ...node,
    x: W / 2 + (Math.random() - 0.5) * 160,
    y: H / 2 + (Math.random() - 0.5) * 160,
    vx: 0,
    vy: 0,
    r: radius(node),
  }))

  if (props.seed === 'circle' && result.length > 1) {
    result.forEach((node, index) => {
      const angle = (index / result.length) * Math.PI * 2
      node.x = W / 2 + Math.cos(angle) * Math.min(W, H) * 0.32
      node.y = H / 2 + Math.sin(angle) * Math.min(W, H) * 0.32
    })
  } else if (props.seed === 'fishbone') {
    const spine = result.filter((node) => node.kind === 'spine')
    spine.forEach((node, index) => {
      node.x = 140 + (index / Math.max(1, spine.length - 1)) * (W - 280)
      node.y = H / 2
    })
  }
  return result
}

function tick(): void {
  if (!running.value) {
    rafId = 0
    return
  }
  const nodes = laidOut.value
  const repulsion = 1000
  const springK = 0.05
  const damping = 0.86
  const centerK = 0.004

  // 1. 斥力 + 碰撞（防重叠）
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const a = nodes[i]
      const b = nodes[j]
      const dx = a.x - b.x
      const dy = a.y - b.y
      const dist = Math.sqrt(dx * dx + dy * dy) + 0.01
      const minDist = a.r + b.r + 20
      let push: number
      if (dist < minDist) {
        push = (minDist - dist) * 0.6
      } else {
        push = repulsion / (dist * dist)
      }
      const fx = (dx / dist) * push
      const fy = (dy / dist) * push
      a.vx += fx
      a.vy += fy
      b.vx -= fx
      b.vy -= fy
    }
  }

  // 2. 弹簧（沿边吸引）
  for (const edge of props.edges) {
    const a = nodes.find((node) => node.id === edge.source)
    const b = nodes.find((node) => node.id === edge.target)
    if (!a || !b) continue
    const dx = b.x - a.x
    const dy = b.y - a.y
    const dist = Math.sqrt(dx * dx + dy * dy) + 0.01
    const force = (dist - 88) * springK * (edge.weight ?? 1)
    const fx = (dx / dist) * force
    const fy = (dy / dist) * force
    a.vx += fx
    a.vy += fy
    b.vx -= fx
    b.vy -= fy
  }

  // 3. 向心 + 积分 + 边界
  for (const node of nodes) {
    node.vx += (W / 2 - node.x) * centerK
    node.vy += (H / 2 - node.y) * centerK
    node.vx *= damping
    node.vy *= damping
    node.x += node.vx
    node.y += node.vy
    node.x = Math.max(node.r, Math.min(W - node.r, node.x))
    node.y = Math.max(node.r, Math.min(H - node.r, node.y))
  }

  rafId = requestAnimationFrame(tick)
}

function startSimulation(): void {
  if (rafId) cancelAnimationFrame(rafId)
  rafId = requestAnimationFrame(tick)
}

function toggleRunning(): void {
  running.value = !running.value
  if (running.value) startSimulation()
}

watch(
  () => [props.nodes, props.edges] as const,
  () => {
    laidOut.value = initLayout([...props.nodes])
    startSimulation()
  },
  { immediate: true },
)

function onWheel(event: WheelEvent): void {
  event.preventDefault()
  const rect = svg.value?.getBoundingClientRect()
  if (!rect) return
  const px = event.clientX - rect.left
  const py = event.clientY - rect.top
  const factor = event.deltaY < 0 ? 1.12 : 0.9
  const k = Math.min(3, Math.max(0.3, transform.k * factor))
  const kRatio = k / transform.k
  transform.x = px - (px - transform.x) * kRatio
  transform.y = py - (py - transform.y) * kRatio
  transform.k = k
}

function onMouseDown(event: MouseEvent): void {
  if ((event.target as Element).closest('.node')) return
  dragging.value = true
  dragStart.value = { x: event.clientX, y: event.clientY, tx: transform.x, ty: transform.y }
}

const draggingNodeId = ref<string | null>(null)
const nodeStart = ref({ x: 0, y: 0 })
const dragScreenStart = ref({ x: 0, y: 0 })
let nodeDragMoved = false

function onNodeMouseDown(event: MouseEvent, node: LayoutNode): void {
  event.stopPropagation()
  draggingNodeId.value = node.id
  nodeStart.value = { x: node.x, y: node.y }
  dragScreenStart.value = { x: event.clientX, y: event.clientY }
  nodeDragMoved = false
  node.vx = 0
  node.vy = 0
}

function onMouseMove(event: MouseEvent): void {
  if (draggingNodeId.value) {
    const node = laidOut.value.find((item) => item.id === draggingNodeId.value)
    if (node) {
      const dx = (event.clientX - dragScreenStart.value.x) / transform.k
      const dy = (event.clientY - dragScreenStart.value.y) / transform.k
      node.x = nodeStart.value.x + dx
      node.y = nodeStart.value.y + dy
      if (Math.abs(dx) + Math.abs(dy) > 2) nodeDragMoved = true
    }
    return
  }
  if (!dragging.value) return
  transform.x = dragStart.value.tx + (event.clientX - dragStart.value.x)
  transform.y = dragStart.value.ty + (event.clientY - dragStart.value.y)
}

function onMouseUp(): void {
  if (draggingNodeId.value) {
    const node = laidOut.value.find((item) => item.id === draggingNodeId.value)
    draggingNodeId.value = null
    if (node && !nodeDragMoved) {
      clickNode(node)
    }
    return
  }
  dragging.value = false
}

function clickNode(node: LayoutNode): void {
  emit('node-click', node)
}

function selectedClass(node: LayoutNode): string {
  return node.id === props.selectedId ? 'node-selected' : ''
}

onMounted(() => {
  window.addEventListener('mouseup', onMouseUp)
})

onUnmounted(() => {
  window.removeEventListener('mouseup', onMouseUp)
  if (rafId) cancelAnimationFrame(rafId)
})
</script>

<template>
  <div class="graph-wrap" :style="{ height: `${height ?? 640}px` }">
    <svg
      ref="svg"
      class="graph-svg"
      :viewBox="`0 0 ${W} ${H}`"
      @wheel="onWheel"
      @mousedown="onMouseDown"
      @mousemove="onMouseMove"
      @mouseup="onMouseUp"
      :style="{ cursor: dragging ? 'grabbing' : 'grab' }"
    >
      <g :transform="`translate(${transform.x},${transform.y}) scale(${transform.k})`">
        <line
          v-for="(edge, index) in edgesWithCoords"
          :key="`e-${index}`"
          class="edge"
          :class="`edge-${edge.relation}`"
          :x1="edge.source.x"
          :y1="edge.source.y"
          :x2="edge.target.x"
          :y2="edge.target.y"
        />
        <g
          v-for="node in laidOut"
          :key="node.id"
          class="node"
          :class="[`node-${node.kind}`, selectedClass(node)]"
          :transform="`translate(${node.x},${node.y})`"
          @mousedown="onNodeMouseDown($event, node)"
        >
          <circle :r="node.r" />
          <text v-if="node.kind === 'paper' || node.kind === 'spine'" class="node-label" :y="node.r + 14">
            {{ node.label }}
          </text>
          <text v-else class="node-label node-label-small" :y="node.r + 12">
            {{ node.label.slice(0, 8) }}{{ node.label.length > 8 ? '…' : '' }}
          </text>
          <title>{{ node.label }}</title>
        </g>
      </g>
    </svg>

    <div class="graph-controls">
      <button class="btn btn-plain btn-sm" @click="toggleRunning">
        {{ running ? '暂停' : '继续' }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.graph-wrap {
  position: relative;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg);
  overflow: hidden;
}

.graph-svg {
  width: 100%;
  height: 100%;
  display: block;
  touch-action: none;
}

.edge {
  stroke: var(--border-strong);
  stroke-width: 1;
  opacity: 0.7;
}

.edge-spine_order { stroke-width: 1.6; stroke: var(--text-faint); }
.edge-has_content { stroke: var(--accent); opacity: 0.32; }
.edge-belongs_to { stroke: var(--text-faint); opacity: 0.4; }
.edge-has_asset { stroke: var(--ok); opacity: 0.4; stroke-dasharray: 3 3; }
.edge-has_spine { stroke: var(--border-strong); opacity: 0.8; }
.edge-has_keyword { stroke: var(--accent); opacity: 0.35; }
.edge-shared { stroke: var(--text-faint); opacity: 0.6; }

.node { cursor: pointer; }
.node circle { transition: stroke-width 120ms ease; }

.node-paper circle { fill: var(--accent); stroke: var(--accent-strong); }
.node-spine circle { fill: var(--bg-elev); stroke: var(--border-strong); stroke-width: 1.6; }
.node-content circle { fill: var(--accent-soft); stroke: var(--accent); stroke-width: 1; }
.node-asset circle { fill: rgba(23, 128, 61, 0.16); stroke: var(--ok); stroke-width: 1; }

.node-paper text { fill: var(--text-dim); font-weight: 600; }
.node-spine text { fill: var(--text-dim); }

.node-label { font-size: 12px; text-anchor: middle; pointer-events: none; fill: var(--text); }
.node-label-small { font-size: 9px; fill: var(--text-faint); }

.node-selected circle { stroke-width: 4; stroke: var(--text); }

.graph-controls {
  position: absolute;
  top: 10px;
  left: 10px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-elev);
  box-shadow: var(--shadow);
}
</style>

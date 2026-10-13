<script setup lang="ts">
/**
 * 渲染 Markdown（OKF 文档、模型回答）。
 *
 * `html: false` 让模型输出与手写 OKF 文件中的原始 HTML 被转义，而不是注入页面。
 * 内部相对链接不交给浏览器/路由器处理，而是以 `link-click` 事件上抛，由父组件
 * 决定如何显示（PDF、图片、另一个概念），从而避免跳转到不存在的 SPA 路由。
 */
import { computed } from 'vue'
import MarkdownIt from 'markdown-it'

const props = defineProps<{ source: string }>()

const emit = defineEmits<{ (e: 'link-click', href: string): void }>()

const md = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: false,
  typographer: false,
})

const rendered = computed(() => md.render(props.source || ''))

function onClick(event: MouseEvent): void {
  const anchor = (event.target as Element | null)?.closest('a')
  if (!anchor) return
  const href = anchor.getAttribute('href') || ''
  if (!href) return
  if (href.startsWith('#')) return
  if (href.startsWith('http://') || href.startsWith('https://') || href.startsWith('mailto:')) {
    event.preventDefault()
    window.open(href, '_blank', 'noopener')
    return
  }
  event.preventDefault()
  emit('link-click', href)
}
</script>

<template>
  <div class="markdown" v-html="rendered" @click="onClick" />
</template>

/** Routing: one route per page, mirroring the sidebar. */

import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/library' },
  {
    path: '/library',
    name: 'library',
    component: () => import('../views/LibraryView.vue'),
    meta: { title: '论文库', subtitle: '检索、筛选与批量选择论文' },
  },
  {
    path: '/ingest',
    name: 'ingest',
    component: () => import('../views/IngestView.vue'),
    meta: { title: '导入', subtitle: '上传 PDF 并跟踪解析入库进度' },
  },
  {
    path: '/paper/:hash',
    name: 'paper',
    component: () => import('../views/PaperView.vue'),
    meta: { title: '论文详情', subtitle: '原文、OKF、图表、金字塔与鱼骨图谱' },
  },
  {
    path: '/qa',
    name: 'qa',
    component: () => import('../views/QaView.vue'),
    meta: { title: '问答', subtitle: '基于金字塔 RAG 的引用式问答' },
  },
  {
    path: '/brainstorm',
    name: 'brainstorm',
    component: () => import('../views/BrainstormView.vue'),
    meta: { title: '头脑风暴', subtitle: '多智能体隔离讨论与主持人汇总' },
  },
  {
    path: '/graph',
    name: 'graph',
    component: () => import('../views/GraphView.vue'),
    meta: { title: '知识图谱', subtitle: '七维鱼骨与跨论文结构' },
  },
  {
    path: '/skills',
    name: 'skills',
    component: () => import('../views/SkillsView.vue'),
    meta: { title: '技能', subtitle: '装载指令与声明式工具' },
  },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('../views/SettingsView.vue'),
    meta: { title: '设置', subtitle: '模型提供方、嵌入方案与运行参数' },
  },
  { path: '/:pathMatch(.*)*', redirect: '/library' },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

export default router

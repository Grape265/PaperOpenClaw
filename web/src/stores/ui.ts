/**
 * Interface state: theme, toasts and a shared busy flag.
 *
 * Toasts are text-only messages, consistent with the no-icons rule.
 */

import { reactive } from 'vue'

export type ToastKind = 'info' | 'ok' | 'warn' | 'error'

export interface Toast {
  id: number
  kind: ToastKind
  message: string
}

const THEME_KEY = 'paperopenclaw.theme'

interface UiState {
  theme: 'dark' | 'light'
  toasts: Toast[]
  sidebarCollapsed: boolean
}

function initialTheme(): 'dark' | 'light' {
  try {
    const stored = localStorage.getItem(THEME_KEY)
    if (stored === 'dark' || stored === 'light') return stored
  } catch {
    /* private mode */
  }
  return 'dark'
}

const state = reactive<UiState>({
  theme: initialTheme(),
  toasts: [],
  sidebarCollapsed: false,
})

let nextId = 1

function applyTheme(): void {
  document.documentElement.setAttribute('data-theme', state.theme)
}

applyTheme()

export const uiStore = {
  state,

  setTheme(theme: 'dark' | 'light'): void {
    state.theme = theme
    applyTheme()
    try {
      localStorage.setItem(THEME_KEY, theme)
    } catch {
      /* ignore */
    }
  },

  toggleTheme(): void {
    this.setTheme(state.theme === 'dark' ? 'light' : 'dark')
  },

  toggleSidebar(): void {
    state.sidebarCollapsed = !state.sidebarCollapsed
  },

  notify(message: string, kind: ToastKind = 'info', timeout = 4200): number {
    const id = nextId++
    state.toasts.push({ id, kind, message })
    if (timeout > 0) {
      window.setTimeout(() => this.dismiss(id), timeout)
    }
    return id
  },

  ok(message: string): number {
    return this.notify(message, 'ok')
  },

  error(message: string): number {
    return this.notify(message, 'error', 7000)
  },

  warn(message: string): number {
    return this.notify(message, 'warn', 5600)
  },

  dismiss(id: number): void {
    const index = state.toasts.findIndex((toast) => toast.id === id)
    if (index >= 0) state.toasts.splice(index, 1)
  },
}

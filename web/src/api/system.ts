/** System endpoints: /health, /doctor, /settings, /provider-presets. */

import { http } from './client'
import type { DoctorReport, HealthReport, ProviderPreset, Settings } from './types'

export const systemApi = {
  health: () => http.get<HealthReport>('/health'),

  doctor: () => http.get<DoctorReport>('/doctor'),

  providerPresets: () => http.get<{ items: ProviderPreset[] }>('/provider-presets'),

  getSettings: () => http.get<Settings>('/settings'),

  updateSettings: (patch: Record<string, unknown>) =>
    http.put<{ settings: Settings; embedding_changed: boolean }>('/settings', patch),

  testProvider: (payload: { provider: string; model?: string; kind?: string }) =>
    http.post<{ provider: string; model: string; kind: string; latency_ms: number; sample?: string }>(
      '/settings/test',
      payload,
    ),

  listModels: (providerId: string) =>
    http.get<{ provider: string; models: string[] }>(`/providers/${providerId}/models`),
}

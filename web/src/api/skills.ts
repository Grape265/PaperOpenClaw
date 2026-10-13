/** Skill endpoints: catalogue, detail, install, enable/disable, uninstall. */

import { http } from './client'
import type { Skill, SkillDetail } from './types'

export const skillsApi = {
  list: () => http.get<{ items: Skill[] }>('/skills'),

  get: (skillId: string) => http.get<SkillDetail>(`/skills/${skillId}`),

  installFromPath: (path: string, enable = true, overwrite = false) =>
    http.post<Skill>('/skills/install', { path, enable, overwrite }),

  installFromZip: (file: File, enable = true, overwrite = false) =>
    http.uploadOne<Skill>('/skills/upload', file, { enable, overwrite }),

  setEnabled: (skillId: string, enabled: boolean) =>
    http.patch<Skill>(`/skills/${skillId}`, { enabled }),

  reload: (skillId: string) => http.post<Skill>(`/skills/${skillId}/reload`),

  remove: (skillId: string) =>
    http.del<{ skill_id: string; deleted: boolean }>(`/skills/${skillId}`),
}

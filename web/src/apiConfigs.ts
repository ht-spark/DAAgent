// 本地保存的 API 配置（localStorage）
// 一次填写，多次复用；并标记一个当前激活的供 Submit 默认填入。

// 思考程度：close 表示不传给模型；其余透传给支持 reasoning_effort 的端点。
// value 是发给接口的原始参数（必须保持英文），label 是给用户看的中文名，两者不要混用。
export type ReasoningEffort = 'close' | 'low' | 'medium' | 'high' | 'xhigh' | 'max'

export type ApiConfig = {
  id: string
  name: string
  provider: 'openai' | 'deepseek' | 'custom'
  apiKey: string
  baseUrl: string
  model: string
  maxTokens?: number
  temperature?: number
  reasoningEffort?: ReasoningEffort
  createdAt: number
}

/** 思考程度下拉选项（接口值 → 中文展示名） */
export const REASONING_EFFORT_OPTIONS: { value: ReasoningEffort; label: string }[] = [
  { value: 'close', label: '关闭' },
  { value: 'low', label: '低' },
  { value: 'medium', label: '中' },
  { value: 'high', label: '高' },
  { value: 'xhigh', label: '极高' },
  { value: 'max', label: '最大' },
]

/** 默认档位：不设置时不向接口传 reasoning_effort */
export const DEFAULT_REASONING_EFFORT: ReasoningEffort = 'close'

export function isReasoningEffort(v: unknown): v is ReasoningEffort {
  return v === 'close' || v === 'low' || v === 'medium' || v === 'high' || v === 'xhigh' || v === 'max'
}

/** 把接口值翻成中文展示名；未设置时回落到默认档位，保证界面不出现英文档位。 */
export function reasoningEffortLabel(v: ReasoningEffort | undefined): string {
  const value = v ?? DEFAULT_REASONING_EFFORT
  return REASONING_EFFORT_OPTIONS.find((o) => o.value === value)?.label ?? value
}

export type ExternalService = 'tavily' | 'mineru'

export type ExternalServiceConfig = {
  apiKey: string
  baseUrl: string
}

export type ApiSettingsSnapshot = {
  configs: ApiConfig[]
  activeId: string | null
  externalServices: Record<ExternalService, ExternalServiceConfig>
}

const KEY_CONFIGS = 'mmagent.apiConfigs'
const KEY_ACTIVE = 'mmagent.apiActiveId'
const KEY_EXTERNAL_SERVICES = 'mmagent.externalServiceConfigs'

export const DEFAULT_EXTERNAL_SERVICES: Record<ExternalService, ExternalServiceConfig> = {
  tavily: { apiKey: '', baseUrl: 'https://api.tavily.com' },
  mineru: { apiKey: '', baseUrl: 'https://mineru.net' },
}

export function loadConfigs(): ApiConfig[] {
  try {
    const raw = localStorage.getItem(KEY_CONFIGS)
    if (!raw) return []
    const arr = JSON.parse(raw)
    if (!Array.isArray(arr)) return []
    return arr.map((config) => ({
      ...config,
      reasoningEffort: config.reasoningEffort === 'off'
        ? 'close'
        : isReasoningEffort(config.reasoningEffort) ? config.reasoningEffort : 'close',
    }))
  } catch {
    return []
  }
}

export function saveConfigs(configs: ApiConfig[]) {
  localStorage.setItem(KEY_CONFIGS, JSON.stringify(configs))
}

export function loadActiveId(): string | null {
  return localStorage.getItem(KEY_ACTIVE)
}

export function saveActiveId(id: string | null) {
  if (id) localStorage.setItem(KEY_ACTIVE, id)
  else localStorage.removeItem(KEY_ACTIVE)
}

export function loadExternalServiceConfigs(): Record<ExternalService, ExternalServiceConfig> {
  try {
    const raw = localStorage.getItem(KEY_EXTERNAL_SERVICES)
    if (!raw) return { ...DEFAULT_EXTERNAL_SERVICES }
    const saved = JSON.parse(raw) as Partial<Record<ExternalService, ExternalServiceConfig>>
    return {
      tavily: { ...DEFAULT_EXTERNAL_SERVICES.tavily, ...saved.tavily },
      mineru: { ...DEFAULT_EXTERNAL_SERVICES.mineru, ...saved.mineru },
    }
  } catch {
    return { ...DEFAULT_EXTERNAL_SERVICES }
  }
}

export function saveExternalServiceConfigs(
  configs: Record<ExternalService, ExternalServiceConfig>,
) {
  localStorage.setItem(KEY_EXTERNAL_SERVICES, JSON.stringify(configs))
}

export function newId(): string {
  return 'cfg_' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4)
}

export function maskKey(k: string): string {
  if (!k) return ''
  if (k.length <= 8) return '••••'
  return k.slice(0, 4) + '••••••••' + k.slice(-4)
}

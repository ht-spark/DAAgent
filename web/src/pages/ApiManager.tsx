import { useEffect, useRef, useState } from 'react'
import {
  ApiConfig,
  loadConfigs,
  saveConfigs,
  newId,
  ExternalService,
  ExternalServiceConfig,
  loadExternalServiceConfigs,
  saveExternalServiceConfigs,
  REASONING_EFFORT_OPTIONS,
  isReasoningEffort,
} from '../apiConfigs'
import { persistApiSettings, syncApiSettingsCache } from '../api'
import ConfirmDialog from '../components/ConfirmDialog'

const EMPTY: Omit<ApiConfig, 'id' | 'createdAt'> = {
  name: '',
  provider: 'openai',
  apiKey: '',
  baseUrl: '',
  model: '',
  maxTokens: 20000,
  temperature: 0.3,
  reasoningEffort: 'off',
}

/** 服务商展示名，卡片与弹窗共用 */
const PROVIDER_LABELS: Record<ApiConfig['provider'], string> = {
  openai: 'OpenAI',
  deepseek: 'DeepSeek',
  custom: '自定义',
}

const SERVICE_META: Record<ExternalService, { name: string; keyLabel: string; hint: string }> = {
  tavily: {
    name: 'Tavily 联网检索',
    keyLabel: 'Tavily API Key',
    hint: '用于联网搜索与知识检索补充。',
  },
  mineru: {
    name: 'MinerU 文档转换',
    keyLabel: 'MinerU Token',
    hint: '用于后续将文档转换为 Markdown。',
  },
}

export default function ApiManager({ embedded = false }: { embedded?: boolean }) {
  const [configs, setConfigs] = useState<ApiConfig[]>([])
  const [editing, setEditing] = useState<Partial<ApiConfig>>(EMPTY)
  const [isNew, setIsNew] = useState(false)
  const [serviceConfigs, setServiceConfigs] = useState<Record<ExternalService, ExternalServiceConfig>>(
    loadExternalServiceConfigs,
  )
  const persistDebounceRef = useRef<number | null>(null)
  const editorOpen = isNew || Boolean(editing.id)
  // 编辑弹窗内联校验错误（替代浏览器 alert）
  const [formError, setFormError] = useState('')
  // 删除配置时的确认弹窗
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null)

  // 弹窗打开时锁定页面滚动，关闭后恢复
  useEffect(() => {
    if (!editorOpen) return
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = prev
    }
  }, [editorOpen])

  // Esc 关闭编辑弹窗
  useEffect(() => {
    if (!editorOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') cancelEdit()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [editorOpen])

  useEffect(() => {
    setConfigs(loadConfigs())
    setServiceConfigs(loadExternalServiceConfigs())
    // 后端本地文件是权威存储：同步成功后覆盖界面与浏览器缓存
    let cancelled = false
    syncApiSettingsCache().then((synced) => {
      if (!synced || cancelled) return
      setConfigs(loadConfigs())
      setServiceConfigs(loadExternalServiceConfigs())
    })
    return () => {
      cancelled = true
    }
  }, [])

  function persist(next: ApiConfig[]) {
    setConfigs(next)
    saveConfigs(next)
    void persistApiSettings({
      configs: next,
      activeId: null,
      externalServices: serviceConfigs,
    })
  }

  function startNew() {
    setEditing({ ...EMPTY })
    setIsNew(true)
    setFormError('')
  }

  function startEdit(c: ApiConfig) {
    setEditing({ ...c })
    setIsNew(false)
    setFormError('')
  }

  function cancelEdit() {
    setEditing({ ...EMPTY })
    setIsNew(false)
    setFormError('')
  }

  function save() {
    const name = (editing.name ?? '').trim() || '未命名配置'
    const provider = (editing.provider ?? 'openai') as ApiConfig['provider']
    const apiKey = editing.apiKey ?? ''
    const baseUrl = editing.baseUrl ?? ''
    const model = editing.model ?? ''
    const maxTokens = Number(editing.maxTokens)
    const temperature = Number(editing.temperature)
    const reasoningEffort = isReasoningEffort(editing.reasoningEffort)
      ? editing.reasoningEffort
      : 'off'

    if (!Number.isInteger(maxTokens) || maxTokens < 1 || maxTokens > 200000) {
      setFormError('最大输出 Token 必须是 1 到 200000 之间的整数。')
      return
    }
    if (!Number.isFinite(temperature) || temperature < 0 || temperature > 2) {
      setFormError('Temperature 必须是 0 到 2 之间的数值。')
      return
    }
    setFormError('')

    if (isNew) {
      const c: ApiConfig = {
        id: newId(),
        name,
        provider,
        apiKey,
        baseUrl,
        model,
        maxTokens,
        temperature,
        reasoningEffort,
        createdAt: Date.now(),
      }
      const next = [...configs, c]
      persist(next)
    } else if (editing.id) {
      const next = configs.map((c) =>
        c.id === editing.id
          ? { ...c, name, provider, apiKey, baseUrl, model, maxTokens, temperature, reasoningEffort }
          : c,
      )
      persist(next)
    }
    cancelEdit()
  }

  function confirmRemove() {
    if (!pendingDeleteId) return
    const next = configs.filter((c) => c.id !== pendingDeleteId)
    persist(next)
    setPendingDeleteId(null)
  }


  /** 扩展服务输入防抖：停止输入 400ms 后才写后端文件 */
  function schedulePersist(snapshot: Parameters<typeof persistApiSettings>[0]) {
    if (persistDebounceRef.current !== null) window.clearTimeout(persistDebounceRef.current)
    persistDebounceRef.current = window.setTimeout(() => {
      persistDebounceRef.current = null
      void persistApiSettings(snapshot)
    }, 400)
  }

  function updateService(
    service: ExternalService,
    field: keyof ExternalServiceConfig,
    value: string,
  ) {
    const next = {
      ...serviceConfigs,
      [service]: { ...serviceConfigs[service], [field]: value },
    }
    setServiceConfigs(next)
    saveExternalServiceConfigs(next)
    schedulePersist({ configs, activeId: null, externalServices: next })
  }

  return (
    <div className={`api-page ${embedded ? 'api-page--embedded' : ''}`}>
      <div className="api-toolbar">
        {!embedded && (
          <div className="page-head-copy">
            <h1 className="page-title">API 管理</h1>
            <div className="page-sub">
              管理 LLM、联网检索和文档转换服务。配置保存在本机，重新进入网页会自动恢复。
            </div>
          </div>
        )}
        <button type="button" className="api-add-btn" onClick={startNew}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" aria-hidden="true">
            <path d="M12 5v14M5 12h14" strokeLinecap="round" />
          </svg>
          新增配置
        </button>
      </div>

      {/* 编辑 / 新增：模态弹窗，点击遮罩或按 Esc 可关闭 */}
      {editorOpen && (
        <div className="api-modal-mask" onClick={cancelEdit} role="presentation">
          <div
            className="api-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="api-editor-title"
            onClick={(e) => e.stopPropagation()}
          >
            <header className="api-modal-head">
              <div>
                <h2 id="api-editor-title">{isNew ? '新增 API 配置' : '编辑 API 配置'}</h2>
                <p className="api-modal-lead">填写服务商、模型与密钥信息；使用哪个模型请在输入框下方选择。</p>
              </div>
              <button
                type="button"
                className="api-modal-close"
                onClick={cancelEdit}
                aria-label="关闭"
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                  <path d="M6 6l12 12M18 6L6 18" strokeLinecap="round" />
                </svg>
              </button>
            </header>

            <div className="api-modal-body">
              {formError ? (
                <p className="api-form-error" role="alert">{formError}</p>
              ) : null}
              <div className="field">
                <label htmlFor="api-name">配置名</label>
                <input
                  id="api-name"
                  type="text"
                  value={editing.name ?? ''}
                  onChange={(e) => setEditing({ ...editing, name: e.target.value })}
                  placeholder="例如：DeepSeek 个人 / OpenAI 官方"
                  autoFocus
                />
              </div>

              <div className="form-row">
                <div className="field">
                  <label htmlFor="api-provider">服务商</label>
                  <select
                    id="api-provider"
                    value={editing.provider ?? 'openai'}
                    onChange={(e) =>
                      setEditing({ ...editing, provider: e.target.value as ApiConfig['provider'] })
                    }
                  >
                    <option value="openai">OpenAI</option>
                    <option value="deepseek">DeepSeek</option>
                    <option value="custom">自定义</option>
                  </select>
                </div>
                <div className="field">
                  <label htmlFor="api-model">模型</label>
                  <input
                    id="api-model"
                    type="text"
                    value={editing.model ?? ''}
                    onChange={(e) => setEditing({ ...editing, model: e.target.value })}
                    placeholder="如 gpt-4o-mini / deepseek-chat"
                  />
                </div>
              </div>

              <div className="field">
                <label htmlFor="api-base-url">Base URL</label>
                <input
                  id="api-base-url"
                  type="url"
                  value={editing.baseUrl ?? ''}
                  onChange={(e) => setEditing({ ...editing, baseUrl: e.target.value })}
                  placeholder="可留空；OpenAI 官方默认；DeepSeek 可填 https://api.deepseek.com"
                />
              </div>

              <div className="field">
                <label htmlFor="api-key">API Key</label>
                <input
                  id="api-key"
                  type="password"
                  value={editing.apiKey ?? ''}
                  onChange={(e) => setEditing({ ...editing, apiKey: e.target.value })}
                  placeholder="sk-..."
                  autoComplete="off"
                />
              </div>

              <div className="form-row">
                <div className="field">
                  <label htmlFor="api-max-tokens">最大输出 Token</label>
                  <input
                    id="api-max-tokens"
                    type="number"
                    min="1"
                    max="200000"
                    step="1"
                    value={editing.maxTokens ?? 20000}
                    onChange={(e) => setEditing({ ...editing, maxTokens: Number(e.target.value) })}
                  />
                </div>
                <div className="field">
                  <label htmlFor="api-temperature">Temperature</label>
                  <input
                    id="api-temperature"
                    type="number"
                    min="0"
                    max="2"
                    step="0.1"
                    value={editing.temperature ?? 0.3}
                    onChange={(e) => setEditing({ ...editing, temperature: Number(e.target.value) })}
                  />
                </div>
              </div>

              <div className="field">
                <label htmlFor="api-reasoning-effort">思考程度</label>
                <select
                  id="api-reasoning-effort"
                  value={isReasoningEffort(editing.reasoningEffort) ? editing.reasoningEffort : 'off'}
                  onChange={(e) => {
                    const v = e.target.value
                    if (isReasoningEffort(v)) setEditing({ ...editing, reasoningEffort: v })
                  }}
                >
                  {REASONING_EFFORT_OPTIONS.map((opt) => (
                    <option key={opt.value} value={opt.value}>
                      {opt.label}
                    </option>
                  ))}
                </select>
                <p className="api-field-hint">
                  仅对支持 reasoning_effort 的模型生效；关闭时不传该参数。
                </p>
              </div>
            </div>

            <footer className="api-modal-foot">
              <button type="button" className="api-cancel" onClick={cancelEdit}>
                取消
              </button>
              <button type="button" className="api-save" onClick={save}>
                保存
              </button>
            </footer>
          </div>
        </div>
      )}

      <section className="api-section" aria-labelledby="model-api-heading">
        <div className="api-section-head">
          <h2 id="model-api-heading">模型服务</h2>
          <p>在这里维护可用模型；实际使用的模型请在输入框下方选择。</p>
        </div>

        {configs.length === 0 ? (
          <div className="api-empty">
            <div className="api-empty-mark" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6">
                <path d="M12 5v14M5 12h14" strokeLinecap="round" />
              </svg>
            </div>
            <p className="api-empty-title">还没有保存任何 API</p>
            <p className="api-empty-desc">添加模型服务后，即可在输入框下方选择使用。</p>
            <button type="button" className="api-btn primary" onClick={startNew}>
              新增配置
            </button>
          </div>
        ) : (
          <div className="api-model-list">
            {configs.map((c) => (
              <div className="api-model-row" key={c.id}>
                <div className="api-model-identity">
                  <span className="api-model-mark" aria-hidden="true">{(c.model || c.name || 'M').slice(0, 1).toUpperCase()}</span>
                  <div className="api-model-copy">
                    <strong title={c.model || c.name}>{c.model || c.name || '未命名模型'}</strong>
                    <span>{PROVIDER_LABELS[c.provider] ?? c.provider}</span>
                  </div>
                </div>
                <div className="api-model-actions">
                  <button
                    type="button"
                    onClick={() => startEdit(c)}
                    aria-label={`编辑 ${c.model || c.name || '模型'}`}
                  >
                    编辑
                  </button>
                  <button
                    type="button"
                    className="danger"
                    onClick={() => setPendingDeleteId(c.id)}
                    aria-label={`删除 ${c.model || c.name || '模型'}`}
                  >
                    删除
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="api-section" aria-labelledby="external-api-heading">
        <div className="api-section-head">
          <h2 id="external-api-heading">扩展服务</h2>
          <p>用于联网检索与文档转换，与模型选择相互独立。</p>
        </div>
        <div className="api-grid">
          {(Object.keys(SERVICE_META) as ExternalService[]).map((service) => {
            const meta = SERVICE_META[service]
            const config = serviceConfigs[service]
            return (
              <div className="api-card" key={service}>
                <div className="api-card-head">
                  <div className="api-name">
                    <span className="api-name-mark" aria-hidden="true">
                      {service === 'tavily' ? 'T' : 'M'}
                    </span>
                    <span className="api-name-text">{meta.name}</span>
                  </div>
                  <span className={`api-badge ${config.apiKey ? '' : 'muted-badge'}`}>
                    {config.apiKey ? '已配置' : '未配置'}
                  </span>
                </div>
                <p className="api-service-hint">{meta.hint}</p>
                <div className="field">
                  <label>{meta.keyLabel}</label>
                  <input
                    type="password"
                    value={config.apiKey}
                    onChange={(event) => updateService(service, 'apiKey', event.target.value)}
                    placeholder={service === 'tavily' ? 'tvly-...' : '输入 MinerU Token'}
                  />
                </div>
                <div className="field">
                  <label>Base URL</label>
                  <input
                    type="url"
                    value={config.baseUrl}
                    onChange={(event) => updateService(service, 'baseUrl', event.target.value)}
                    placeholder="可使用默认地址"
                  />
                </div>
              </div>
            )
          })}
        </div>
      </section>
      <ConfirmDialog
        open={Boolean(pendingDeleteId)}
        title="删除 API 配置？"
        description="删除后该模型将从输入框下方的可选列表中移除，已保存的对话不受影响。"
        detail={configs.find((c) => c.id === pendingDeleteId)?.name || undefined}
        confirmLabel="删除配置"
        tone="danger"
        onConfirm={confirmRemove}
        onCancel={() => setPendingDeleteId(null)}
      />
    </div>
  )
}

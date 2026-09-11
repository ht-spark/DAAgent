import { memo, useEffect, useRef, useState } from 'react'

import {
  fetchBrainstormDiscussion,
  getKnowledgeBases,
  renameBrainstormDiscussion,
  streamBrainstormMessage,
  streamDataAgentMessage,
  syncApiSettingsCache,
  type AgentTraceEvent,
  type BrainstormSource,
  type KnowledgeBaseSummary,
} from '../api'
import { loadActiveId, loadConfigs, type ApiConfig } from '../apiConfigs'
import MarkdownContent from '../components/MarkdownContent'
import ChatModeling from './ChatModeling'

type Mode = 'inspiration' | 'agent' | 'modeling'

type Message = {
  role: 'assistant' | 'user'
  content: string
  sources?: BrainstormSource[]
  trace?: AgentTraceEvent[]
}

function describeObservation(event: Extract<AgentTraceEvent, { type: 'agent_observation' }>): string {
  if (!event.success) return event.error || '工具执行失败'
  const summary = event.summary
  if (!summary) return `工具执行完成，耗时 ${event.duration_ms} ms`
  const missingRate = typeof summary.missing_rate === 'number'
    ? `，缺失率 ${(summary.missing_rate * 100).toFixed(1)}%`
    : ''
  return `${summary.file_count ?? 0} 个文件，${summary.table_count ?? 0} 张表，${summary.row_count ?? 0} 行，${summary.column_count ?? 0} 列${missingRate}`
}

const AgentInlineProcess = memo(function AgentInlineProcess({
  trace,
  running,
}: {
  trace: AgentTraceEvent[]
  running: boolean
}) {
  return (
    <div className={`agent-inline-process ${running ? 'is-running' : ''}`}>
      <div className="agent-inline-head">
        <span className="agent-inline-status" aria-hidden="true" />
        <strong>{running ? 'Agent 正在执行' : 'Agent 执行过程'}</strong>
        <small>{trace.length} 条事件</small>
      </div>
      <div className="agent-inline-steps">
        {trace.map((event, index) => (
          <div
            className="agent-inline-step"
            key={`${event.type}-${event.type === 'agent_started' ? event.run_id : event.tool_name}-${index}`}
          >
            <span>
              {event.type === 'agent_started'
                ? '任务已接收'
                : event.type === 'agent_action' ? '思考摘要' : event.success ? '观察结果' : '执行失败'}
            </span>
            <div>
              {event.type === 'agent_started' ? (
                <>
                  <p>{event.files.length ? `已授权 ${event.files.length} 个数据文件` : '当前任务没有数据附件'}</p>
                  <code>运行标识 · {event.run_id.slice(0, 8)}</code>
                </>
              ) : event.type === 'agent_action' ? (
                <>
                  <p>{event.thought || '准备执行数据工具'}</p>
                  <code>调用工具 · {event.tool_name}</code>
                </>
              ) : (
                <>
                  <p>{describeObservation(event)}</p>
                  <code>{event.tool_name} · {event.duration_ms} ms</code>
                </>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
})

const ChatMessage = memo(function ChatMessage({
  message,
  onSourceClick,
  activeSourceKey,
  running = false,
}: {
  message: Message
  onSourceClick?: (key: string) => void
  activeSourceKey?: string | null
  running?: boolean
}) {
  if (!message.content && !message.trace?.length) return null
  return (
    <div className={`rag-msg ${message.role}`}>
      {message.role === 'assistant' && <span className="rag-avatar">M</span>}
      <div className={`rag-msg-body ${message.role === 'user' ? 'bubble-user' : ''}`}>
        {message.role === 'assistant' ? (
          <div className="rag-answer">
            {message.trace && message.trace.length > 0 && (
              <AgentInlineProcess trace={message.trace} running={running} />
            )}
            {message.content && <MarkdownContent content={message.content} />}
          </div>
        ) : (
          <div className="rag-msg-plain">{message.content}</div>
        )}
        {message.sources && message.sources.length > 0 && (
          <div className="rag-hit-row">
            {message.sources.map((source, sourceIndex) => {
              const key = `${source.document_id || 'doc'}-${sourceIndex}`
              return (
                <button
                  type="button"
                  key={key}
                  className={`rag-hit ${activeSourceKey === key ? 'active' : ''}`}
                  onClick={() => onSourceClick?.(key)}
                >
                  [{sourceIndex + 1}] {source.source_file || '未命名文档'}
                </button>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
})

type Props = {
  discussionId: string | null
  onDiscussionId: (id: string) => void
  initialMode?: Mode
  initialProblem?: string
  resumeRunId?: string | null
  onViewResult: (runId: string) => void
  onOpenHistory?: () => void
  onNewChat?: () => void
}

export default function NewChat({
  discussionId,
  onDiscussionId,
  initialMode = 'inspiration',
  initialProblem = '',
  resumeRunId,
  onViewResult,
  onOpenHistory,
  onNewChat,
}: Props) {
  const [mode, setMode] = useState<Mode>(resumeRunId ? 'modeling' : initialMode)
  const [draft, setDraft] = useState('')
  const [messages, setMessages] = useState<Message[]>([])
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [files, setFiles] = useState<File[]>([])
  const [title, setTitle] = useState('新讨论')
  const [activeSourceKey, setActiveSourceKey] = useState<string | null>(null)
  const [activeSources, setActiveSources] = useState<BrainstormSource[]>([])
  const [agentTrace, setAgentTrace] = useState<AgentTraceEvent[]>([])
  const [agentRunId, setAgentRunId] = useState<string | null>(null)
  const [sourcesCollapsed, setSourcesCollapsed] = useState(false)
  const [apiConfigs, setApiConfigs] = useState<ApiConfig[]>([])
  const [selectedConfigId, setSelectedConfigId] = useState<string>('')
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseSummary[]>([])
  const [knowledgeBaseIds, setKnowledgeBaseIds] = useState<string[]>(['default'])
  const fileInputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)

  function applyApiConfigs(configs: ApiConfig[], activeId: string | null) {
    setApiConfigs(configs)
    if (configs.length === 0) {
      setSelectedConfigId('')
      return
    }
    const preferred = configs.find((c) => c.id === activeId) ?? configs[0]
    setSelectedConfigId((current) =>
      configs.some((c) => c.id === current) ? current : preferred.id,
    )
  }

  useEffect(() => {
    let cancelled = false
    applyApiConfigs(loadConfigs(), loadActiveId())
    void syncApiSettingsCache().then((synced) => {
      if (!synced || cancelled) return
      applyApiConfigs(loadConfigs(), loadActiveId())
    })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    void getKnowledgeBases().then((bases) => {
      setKnowledgeBases(bases)
      setKnowledgeBaseIds((current) => {
        const available = current.filter((id) => bases.some((base) => base.id === id))
        return available.length ? available : [bases[0]?.id ?? 'default']
      })
    })
  }, [])

  useEffect(() => {
    if (!discussionId) {
      setMessages([])
      setTitle('新讨论')
      setActiveSources([])
      setActiveSourceKey(null)
      setAgentTrace([])
      setAgentRunId(null)
      return
    }
    void fetchBrainstormDiscussion(discussionId)
      .then((discussion) => {
        const savedMetadata = [...discussion.messages]
          .reverse()
          .find((message) => message.metadata?.knowledge_base_ids?.length || message.metadata?.knowledge_base_id)
          ?.metadata
        const savedRoutes = savedMetadata?.knowledge_base_ids
          ?? (savedMetadata?.knowledge_base_id ? [savedMetadata.knowledge_base_id] : [])
        if (savedRoutes.length) setKnowledgeBaseIds(savedRoutes)
        const latestModeMessage = [...discussion.messages]
          .reverse()
          .find((message) => message.metadata?.mode)
        if (latestModeMessage?.metadata?.mode === 'agent') {
          setMode('agent')
          setAgentTrace(latestModeMessage.metadata.agent_trace ?? [])
          setAgentRunId(latestModeMessage.metadata.agent_run_id ?? null)
        } else {
          setMode('inspiration')
          setAgentTrace([])
          setAgentRunId(null)
        }
        setMessages(
          discussion.messages.map((m) => ({
            role: m.role,
            content: m.content,
            sources: m.sources?.length ? m.sources : undefined,
            trace: m.metadata?.agent_trace,
          })),
        )
        setTitle(discussion.title)
        const lastWithSources = [...discussion.messages].reverse().find((m) => m.sources?.length)
        setActiveSources(lastWithSources?.sources ?? [])
        setActiveSourceKey(null)
      })
      .catch((error) =>
        setMessages([
          {
            role: 'assistant',
            content: error instanceof Error ? error.message : '加载讨论记录失败。',
          },
        ]),
      )
  }, [discussionId])

  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages])

  function selectMode(nextMode: Exclude<Mode, 'modeling'>) {
    if (nextMode === mode) return
    setMode(nextMode)
    setFiles([])
    if (fileInputRef.current) fileInputRef.current.value = ''
  }

  async function submit() {
    const message = draft.trim()
    if ((!message && files.length === 0) || isSubmitting) return
    setDraft('')
    setIsSubmitting(true)
    const attachedFiles = files
    setFiles([])
    if (fileInputRef.current) fileInputRef.current.value = ''
    setMessages((current) => [
      ...current,
      { role: 'user', content: message },
      { role: 'assistant', content: '' },
    ])
    try {
      if (mode === 'agent') {
        setAgentTrace([])
        setActiveSources([])
        await streamDataAgentMessage(
          message,
          discussionId,
          title,
          attachedFiles,
          {
            onStarted: (runId) => setAgentRunId(runId),
            onTrace: (event) => {
              setAgentTrace((current) => [...current, event])
              setMessages((current) => current.map(
                (item, index) => index === current.length - 1
                  ? { ...item, trace: [...(item.trace ?? []), event] }
                  : item,
              ))
            },
            onToken: (content) => {
              setMessages((current) => current.map(
                (item, index) => index === current.length - 1
                  ? { ...item, content: `${item.content}${content}` }
                  : item,
              ))
            },
            onDone: (savedDiscussionId, runId) => {
              setAgentRunId(runId)
              onDiscussionId(savedDiscussionId)
            },
          },
          selectedConfigId || null,
        )
      } else {
        await streamBrainstormMessage(
          message,
          discussionId,
          title,
          attachedFiles,
          {
            onSources: (sources) => {
              setActiveSources(sources)
              setActiveSourceKey(null)
              setMessages((current) =>
                current.map((item, index) =>
                  index === current.length - 1 ? { ...item, sources } : item,
                ),
              )
            },
            onToken: (content) => {
              setMessages((current) =>
                current.map((item, index) =>
                  index === current.length - 1
                    ? { ...item, content: `${item.content}${content}` }
                    : item,
                ),
              )
            },
            onDone: onDiscussionId,
          },
          selectedConfigId || null,
          knowledgeBaseIds,
        )
      }
    } catch (error) {
      setMessages((current) => current.map(
        (item, index) => index === current.length - 1
          ? {
              ...item,
              role: 'assistant',
              content: error instanceof Error
                ? error.message
                : mode === 'agent' ? '数据 Agent 暂不可用。' : '灵感讨论服务暂不可用。',
            }
          : item,
      ))
    } finally {
      setIsSubmitting(false)
    }
  }

  async function saveTitle() {
    const normalizedTitle = title.trim() || '新讨论'
    setTitle(normalizedTitle)
    if (discussionId) await renameBrainstormDiscussion(discussionId, normalizedTitle)
  }

  if (mode === 'modeling') {
    return (
      <div className="page page-fill chat-page new-chat-mode">
        <div className="new-chat-mode-bar">
          <button type="button" className="rag-btn" onClick={() => setMode('inspiration')}>
            ← 返回灵感讨论
          </button>
          <span className="new-chat-mode-hint">建模任务 · 上传资料后启动完整建模流程</span>
          {onOpenHistory && (
            <button type="button" className="rag-btn" onClick={onOpenHistory}>
              历史讨论
            </button>
          )}
        </div>
        <ChatModeling
          key={resumeRunId ?? 'modeling'}
          resumeRunId={resumeRunId}
          initialProblem={initialProblem}
          onViewResult={onViewResult}
          knowledgeBases={knowledgeBases}
          knowledgeBaseIds={knowledgeBaseIds}
          onKnowledgeBaseIdsChange={setKnowledgeBaseIds}
        />
      </div>
    )
  }

  const lastAssistantSources = activeSources

  return (
    <div className="page-fill new-chat-page rag-agent" aria-label="新建聊天">
      <header className="rag-chat-top">
        <div className="rag-chat-title">
          <input
            className="rag-title-input"
            value={title}
            maxLength={80}
            onChange={(event) => setTitle(event.target.value)}
            onBlur={() => void saveTitle()}
            aria-label="讨论名称"
          />
          <p>{mode === 'agent' ? '思考 · 执行 · 观察 · 数据画像' : '混合检索 · 引用可追溯 · 灵感迸发'}</p>
        </div>
        <div className="rag-top-actions">
          {onOpenHistory && (
            <button type="button" className="rag-btn" onClick={onOpenHistory}>
              历史讨论
            </button>
          )}
          {onNewChat && (
            <button type="button" className="rag-btn" onClick={onNewChat}>
              新会话
            </button>
          )}
        </div>
      </header>

      <div className={`rag-chat-body ${sourcesCollapsed ? 'sources-collapsed' : ''}`}>
        <section className="rag-chat-col" aria-label="聊天消息">
          <div className="rag-messages" ref={listRef}>
            <div className="rag-messages-inner">
              {messages.map((message, index) => (
                <ChatMessage
                  key={`${message.role}-${index}`}
                  message={message}
                  activeSourceKey={activeSourceKey}
                  onSourceClick={(key) => setActiveSourceKey(key)}
                  running={mode === 'agent' && isSubmitting && index === messages.length - 1}
                />
              ))}
              {messages.length === 0 && mode === 'agent' && (
                <div className="agent-empty-state">
                  <span>DATA AGENT</span>
                  <h2>从一份真实数据开始</h2>
                  <p>上传 CSV、Excel 或 JSON，Agent 会先读取数据，再返回字段质量、分布特征与下一步建议。</p>
                </div>
              )}
              {isSubmitting
                && messages[messages.length - 1]?.content === ''
                && (mode !== 'agent' || agentTrace.length === 0) && (
                <div className="rag-msg assistant">
                  <span className="rag-avatar">M</span>
                  <div className="rag-msg-body">
                    <span className="rag-typing" aria-label={mode === 'agent' ? 'Agent 运行中' : '检索中'}>
                      <i />
                      <i />
                      <i />
                    </span>
                  </div>
                </div>
              )}
            </div>
          </div>

          <div className="rag-composer-wrap">
            <div className="rag-composer">
              {files.length > 0 && (
                <div className="rag-file-chips">
                  {files.map((file, index) => (
                    <span className="rag-file-chip" key={`${file.name}-${index}`}>
                      {file.name}
                      <button
                        type="button"
                        aria-label="移除附件"
                        onClick={() => setFiles((list) => list.filter((_, i) => i !== index))}
                        disabled={isSubmitting}
                      >
                        ×
                      </button>
                    </span>
                  ))}
                </div>
              )}
              <textarea
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && !event.shiftKey) {
                    event.preventDefault()
                    void submit()
                  }
                }}
                placeholder={mode === 'agent' ? '描述希望 Agent 完成的数据分析任务…' : '输入待讨论的建模问题、假设或分析方向…'}
                rows={2}
              />
              <div className="rag-composer-bar">
                <div className="rag-mode-switch" role="group" aria-label="选择工作模式">
                  <button
                    type="button"
                    className={mode === 'inspiration' ? 'active' : ''}
                    onClick={() => selectMode('inspiration')}
                    disabled={isSubmitting}
                  >
                    知识库
                  </button>
                  <button
                    type="button"
                    className={mode === 'agent' ? 'active' : ''}
                    onClick={() => selectMode('agent')}
                    disabled={isSubmitting}
                  >
                    数据 Agent
                  </button>
                </div>
                {mode === 'inspiration' && <details className="rag-kb-picker">
                  <summary title="选择本轮检索使用的知识库">
                    {knowledgeBaseIds.length === 1
                      ? knowledgeBases.find((base) => base.id === knowledgeBaseIds[0])?.name ?? '选择知识库'
                      : `已选 ${knowledgeBaseIds.length} 个知识库`}
                    <span className="rag-kb-chevron" aria-hidden="true" />
                  </summary>
                  <div className="rag-kb-menu">
                    {knowledgeBases.map((base) => (
                      <label key={base.id}>
                        <input
                          type="checkbox"
                          checked={knowledgeBaseIds.includes(base.id)}
                          disabled={isSubmitting}
                          onChange={() => setKnowledgeBaseIds((current) => {
                            if (current.includes(base.id)) {
                              return current.length === 1
                                ? current
                                : current.filter((id) => id !== base.id)
                            }
                            return [...current, base.id]
                          })}
                        />
                        <span>{base.name}</span>
                        <small>{base.document_count}</small>
                      </label>
                    ))}
                  </div>
                </details>}
                <button
                  type="button"
                  className="rag-btn"
                  onClick={() => fileInputRef.current?.click()}
                  disabled={isSubmitting}
                >
                  {mode === 'agent' ? '添加数据' : '上传文件'}{files.length ? ` · ${files.length}` : ''}
                </button>
                <input
                  ref={fileInputRef}
                  hidden
                  type="file"
                  multiple
                  accept={mode === 'agent'
                    ? '.csv,.xlsx,.xls,.json,.jsonl,.ndjson'
                    : '.md,.markdown,.txt,.json,.csv,.xlsx,.xlsm,.png,.jpg,.jpeg,.webp,.gif'}
                  onChange={(event) => setFiles(Array.from(event.target.files ?? []))}
                />
                {apiConfigs.length > 0 ? (
                  <label className="rag-model-select" title="选择对话使用的模型（来自 API 设置）">
                    <select
                      value={selectedConfigId}
                      onChange={(event) => setSelectedConfigId(event.target.value)}
                      disabled={isSubmitting}
                      aria-label="选择模型"
                    >
                      {apiConfigs.map((config) => (
                        <option key={config.id} value={config.id}>
                          {config.model || config.name || '未命名模型'}
                        </option>
                      ))}
                    </select>
                    <svg
                      className="rag-model-chevron"
                      width="12"
                      height="12"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2.2"
                      aria-hidden="true"
                    >
                      <path d="M6 9l6 6 6-6" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                  </label>
                ) : (
                  <span className="rag-model-empty" title="尚未在 API 管理中配置模型">
                    未配置模型
                  </span>
                )}
                <button
                  type="button"
                  className="rag-send"
                  onClick={() => void submit()}
                  disabled={(!draft.trim() && files.length === 0) || isSubmitting}
                  aria-label="发送"
                >
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M5 12h14M13 6l6 6-6 6" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                </button>
              </div>
            </div>
          </div>
        </section>

        <aside
          className={`rag-sources ${sourcesCollapsed ? 'is-collapsed' : ''}`}
          aria-label={mode === 'agent' ? 'Agent 运行轨迹' : '引用来源'}
        >
          {sourcesCollapsed ? (
            <button
              type="button"
              className="rag-sources-expand"
              onClick={() => setSourcesCollapsed(false)}
              title={mode === 'agent' ? '展开运行轨迹' : '展开引用来源'}
            >
              <span className="rag-sources-expand-text">
                {mode === 'agent' ? '运行轨迹' : '引用来源'}
              </span>
              {(mode === 'agent' ? agentTrace.length : activeSources.length) > 0 && (
                <span className="rag-sources-expand-badge">
                  {mode === 'agent' ? agentTrace.length : activeSources.length}
                </span>
              )}
            </button>
          ) : (
            <>
              <div className="rag-sources-head">
                <div>
                  <h2>{mode === 'agent' ? 'Agent 运行轨迹' : '引用来源'}</h2>
                  <p>{mode === 'agent' ? '仅展示可公开的进度与工具结果' : '点击消息中的引用编号定位片段'}</p>
                </div>
                <button
                  type="button"
                  className="rag-sources-collapse"
                  onClick={() => setSourcesCollapsed(true)}
                  title="收起引用来源"
                  aria-label="收起引用来源"
                >
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M9 18l6-6-6-6" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                </button>
              </div>
              <div className="rag-sources-list">
                {mode === 'agent' ? (
                  agentTrace.length === 0 ? (
                    <p className="rag-sources-empty">
                      {isSubmitting ? '正在启动 Agent，等待第一步决策…' : '启动数据 Agent 后，这里会依次展示思考、执行与观察。'}
                    </p>
                  ) : agentTrace.map((event, index) => (
                    <article
                      className={`agent-trace-card ${event.type === 'agent_observation' && !event.success ? 'failed' : ''}`}
                      key={`${event.type}-${event.type === 'agent_started' ? event.run_id : event.tool_name}-${index}`}
                    >
                      <div className="agent-trace-top">
                        <span className={`agent-trace-dot ${event.type === 'agent_started' || event.type === 'agent_action' ? 'thinking' : event.success ? 'success' : 'failed'}`} />
                        <span>
                          {event.type === 'agent_started'
                            ? '任务已接收'
                            : event.type === 'agent_action' ? '思考并执行' : event.success ? '观察完成' : '观察失败'}
                        </span>
                        {event.type === 'agent_observation' && <small>{event.duration_ms} ms</small>}
                      </div>
                      <strong>
                        {event.type === 'agent_started'
                          ? '准备数据分析'
                          : event.tool_name === 'profile_data' ? '基础数据画像' : event.tool_name}
                      </strong>
                      <p>
                        {event.type === 'agent_started'
                          ? event.files.length ? `已授权 ${event.files.length} 个数据文件` : '当前任务没有数据附件'
                          : event.type === 'agent_action' ? event.thought || '准备调用工具' : describeObservation(event)}
                      </p>
                    </article>
                  ))
                ) : lastAssistantSources.length === 0 ? (
                  <p className="rag-sources-empty">发送问题后，这里会展示知识库命中片段。</p>
                ) : (
                  lastAssistantSources.map((source, index) => {
                    const key = `${source.document_id || 'doc'}-${index}`
                    return (
                      <article
                        key={key}
                        className={`rag-source-card ${activeSourceKey === key ? 'active' : ''}`}
                        onClick={() => setActiveSourceKey(key)}
                      >
                        <div className="rag-source-top">
                          <span className="rag-source-id">[{index + 1}]</span>
                          <span className="rag-source-name">{source.source_file || '未命名文档'}</span>
                        </div>
                        <div className="rag-source-snippet">
                          <MarkdownContent content={source.content} />
                        </div>
                      </article>
                    )
                  })
                )}
              </div>
              <div className="rag-metrics">
                {mode === 'agent' ? (
                  <>
                    <div className="rag-metric">
                      <b>{agentTrace.filter((event) => event.type === 'agent_action').length || '···'}</b>
                      <span>执行步骤</span>
                    </div>
                    <div className="rag-metric">
                      <b>{agentTrace.filter((event) => event.type === 'agent_observation' && event.success).length || '···'}</b>
                      <span>成功观察</span>
                    </div>
                    <div className="rag-metric agent-run-metric">
                      <b>{agentRunId ? agentRunId.slice(0, 8) : '···'}</b>
                      <span>运行标识</span>
                    </div>
                  </>
                ) : (
                  <>
                    <div className="rag-metric">
                      <b>{lastAssistantSources.length || '···'}</b>
                      <span>命中片段</span>
                    </div>
                    <div className="rag-metric">
                      <b>{Math.max(0, messages.filter((m) => m.role === 'user').length) || '···'}</b>
                      <span>提问轮次</span>
                    </div>
                    <div className="rag-metric">
                      <b>···</b>
                      <span>检索轮次</span>
                    </div>
                    <div className="rag-metric">
                      <b>···</b>
                      <span>端到端耗时</span>
                    </div>
                  </>
                )}
              </div>
            </>
          )}
        </aside>
      </div>
    </div>
  )
}

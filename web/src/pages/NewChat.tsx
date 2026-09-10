import { memo, useEffect, useRef, useState } from 'react'

import {
  fetchBrainstormDiscussion,
  renameBrainstormDiscussion,
  streamBrainstormMessage,
  type BrainstormSource,
} from '../api'
import MarkdownContent from '../components/MarkdownContent'
import ChatModeling from './ChatModeling'

type Mode = 'inspiration' | 'modeling'

type Message = {
  role: 'assistant' | 'user'
  content: string
  sources?: BrainstormSource[]
}

const WELCOME_MESSAGE: Message = {
  role: 'assistant',
  content:
    '你好，我是 MMAgent 灵感助手。请输入建模问题、已有假设或分析方向，我会从知识库检索相关片段并附引用。',
}

const ChatMessage = memo(function ChatMessage({
  message,
  onSourceClick,
  activeSourceKey,
}: {
  message: Message
  onSourceClick?: (key: string) => void
  activeSourceKey?: string | null
}) {
  if (!message.content) return null
  return (
    <div className={`rag-msg ${message.role}`}>
      {message.role === 'assistant' && <span className="rag-avatar">M</span>}
      <div className={`rag-msg-body ${message.role === 'user' ? 'bubble-user' : ''}`}>
        {message.role === 'assistant' ? (
          <div className="rag-answer">
            <MarkdownContent content={message.content} />
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
  const [messages, setMessages] = useState<Message[]>([WELCOME_MESSAGE])
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [files, setFiles] = useState<File[]>([])
  const [title, setTitle] = useState('新讨论')
  const [activeSourceKey, setActiveSourceKey] = useState<string | null>(null)
  const [activeSources, setActiveSources] = useState<BrainstormSource[]>([])
  const [sourcesCollapsed, setSourcesCollapsed] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!discussionId) {
      setMessages([WELCOME_MESSAGE])
      setTitle('新讨论')
      setActiveSources([])
      setActiveSourceKey(null)
      return
    }
    void fetchBrainstormDiscussion(discussionId)
      .then((discussion) => {
        setMessages(
          discussion.messages.map((m) => ({
            role: m.role,
            content: m.content,
            sources: m.sources?.length ? m.sources : undefined,
          })),
        )
        setTitle(discussion.title)
        const lastWithSources = [...discussion.messages].reverse().find((m) => m.sources?.length)
        setActiveSources(lastWithSources?.sources ?? [])
        setActiveSourceKey(null)
      })
      .catch((error) =>
        setMessages([
          WELCOME_MESSAGE,
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
      await streamBrainstormMessage(message, discussionId, title, attachedFiles, {
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
      })
    } catch (error) {
      setMessages((current) => [
        ...current.slice(0, -1),
        {
          role: 'assistant',
          content: error instanceof Error ? error.message : '灵感讨论服务暂不可用。',
        },
      ])
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
          <p>混合检索 · 引用可追溯 · 灵感迸发</p>
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
                />
              ))}
              {isSubmitting && messages[messages.length - 1]?.content === '' && (
                <div className="rag-msg assistant">
                  <span className="rag-avatar">M</span>
                  <div className="rag-msg-body">
                    <span className="rag-typing" aria-label="检索中">
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
                placeholder="基于知识库提问，例如：我们的退款政策和竞品相比有什么优势？"
                rows={2}
              />
              <div className="rag-composer-bar">
                <button
                  type="button"
                  className="rag-btn"
                  title="多知识库选择接口尚未接入"
                  onClick={() => fileInputRef.current?.click()}
                  disabled={isSubmitting}
                >
                  知识库
                </button>
                <button
                  type="button"
                  className="rag-btn"
                  onClick={() => fileInputRef.current?.click()}
                  disabled={isSubmitting}
                >
                  上传文件{files.length ? ` · ${files.length}` : ''}
                </button>
                <input
                  ref={fileInputRef}
                  hidden
                  type="file"
                  multiple
                  accept=".md,.markdown,.txt,.json,.csv,.xlsx,.xlsm,.png,.jpg,.jpeg,.webp,.gif"
                  onChange={(event) => setFiles(Array.from(event.target.files ?? []))}
                />
                <span className="rag-composer-hint">回答仅基于已索引文档，结论附引用</span>
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

        <aside className={`rag-sources ${sourcesCollapsed ? 'is-collapsed' : ''}`} aria-label="引用来源">
          {sourcesCollapsed ? (
            <button
              type="button"
              className="rag-sources-expand"
              onClick={() => setSourcesCollapsed(false)}
              title="展开引用来源"
            >
              <span className="rag-sources-expand-text">引用来源</span>
              {activeSources.length > 0 && (
                <span className="rag-sources-expand-badge">{activeSources.length}</span>
              )}
            </button>
          ) : (
            <>
              <div className="rag-sources-head">
                <div>
                  <h2>引用来源</h2>
                  <p>点击消息中的引用编号定位片段</p>
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
                {lastAssistantSources.length === 0 ? (
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
                <div className="rag-metric">
                  <b>{lastAssistantSources.length || '—'}</b>
                  <span>命中片段</span>
                </div>
                <div className="rag-metric">
                  <b>{Math.max(0, messages.filter((m) => m.role === 'user').length) || '—'}</b>
                  <span>提问轮次</span>
                </div>
                <div className="rag-metric">
                  <b>—</b>
                  <span>检索轮次</span>
                </div>
                <div className="rag-metric">
                  <b>—</b>
                  <span>端到端耗时</span>
                </div>
              </div>
            </>
          )}
        </aside>
      </div>
    </div>
  )
}

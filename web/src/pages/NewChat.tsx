import { memo, useEffect, useMemo, useRef, useState, type ClipboardEvent } from 'react'

import {
  cancelAgentChatRun,
  decideAgentCommand,
  fetchActiveAgentChatRun,
  fetchBrainstormDiscussion,
  fetchProjectFileTree,
  getKnowledgeBases,
  openProjectFolder,
  renameBrainstormDiscussion,
  reconnectAgentChatRun,
  streamDataAgentMessage,
  syncApiSettingsCache,
  type AgentTraceEvent,
  type AgentChatHandlers,
  type ContextUsage,
  type KnowledgeBaseSummary,
  type KnowledgeSourceCitation,
  type ProjectFileNode,
} from '../api'
import { loadConfigs, type ApiConfig } from '../apiConfigs'
import CommandApprovalDialog from '../components/CommandApprovalDialog'
import MarkdownContent from '../components/MarkdownContent'
import ChatModeling from './ChatModeling'

type Mode = 'agent' | 'modeling'

/** 待人工批准的项目命令请求（对话流内弹窗，不再使用浏览器 confirm）。 */
type ApprovalRequest = {
  approval_id: string
  command: string
  workspace_root: string
  python_environment?: string | null
}

type Message = {
  role: 'assistant' | 'user'
  content: string
  trace?: AgentTraceEvent[]
  /** 本轮检索命中的知识库来源（可收起标注） */
  sources?: KnowledgeSourceCitation[]
  contextUsage?: ContextUsage
}

function ContextUsageRing({ usage }: { usage?: ContextUsage }) {
  const percent = usage && usage.threshold_tokens > 0
    ? Math.min(100, Math.max(0, usage.estimated_tokens / usage.threshold_tokens * 100))
    : 0
  const compactTokens = (tokens: number) => `${(tokens / 1000).toFixed(1)}K`
  const label = usage
    ? `上下文历史估算已用 ${percent.toFixed(1)}%，约 ${usage.estimated_tokens.toLocaleString('zh-CN')} / ${usage.threshold_tokens.toLocaleString('zh-CN')} token，剩余约 ${usage.remaining_tokens.toLocaleString('zh-CN')} token。仅按对话历史估算，不含系统提示和工具调用。`
    : '完成一轮 Agent 对话后显示上下文历史估算用量。'

  return (
    <span className="rag-context-indicator" tabIndex={0} role="img" aria-label={label}>
      <span
        className="rag-context-ring"
        style={{ background: `conic-gradient(var(--rag-accent) ${percent}%, var(--rag-border-strong) 0)` }}
        aria-hidden="true"
      />
      <span className="rag-context-tooltip" aria-hidden="true">
        {usage ? (
          <>
            <span>{percent.toFixed(1)}% · {compactTokens(usage.estimated_tokens)} / {compactTokens(usage.threshold_tokens)} 上下文已用</span>
            <small>剩余约 {compactTokens(usage.remaining_tokens)}，仅为对话历史估算</small>
          </>
        ) : '完成一轮 Agent 对话后显示上下文用量'}
      </span>
    </span>
  )
}

/** 从 trace 中汇总本轮知识库检索来源，按文件去重。 */
function collectKnowledgeSources(trace: AgentTraceEvent[] | undefined): KnowledgeSourceCitation[] {
  if (!trace?.length) return []
  const seen = new Set<string>()
  const sources: KnowledgeSourceCitation[] = []
  for (const event of trace) {
    if (event.type !== 'agent_observation' || !event.sources?.length) continue
    for (const source of event.sources) {
      const key = `${source.document_id ?? ''}::${source.source_file}`
      if (seen.has(key)) continue
      seen.add(key)
      sources.push(source)
    }
  }
  return sources
}

const CLIPBOARD_IMAGE_EXTENSIONS: Record<string, string> = {
  'image/gif': 'gif',
  'image/jpeg': 'jpg',
  'image/png': 'png',
  'image/webp': 'webp',
}

const MAX_OBSERVATION_ERROR_LENGTH = 220

function summarizeObservationError(error: string | null | undefined): string {
  if (!error) return '未提供错误详情'

  const errorSection = error
    .split(/(?:---\s*)?(?:执行|运行)输出(?:\s*---)?\s*[:：]?/i, 1)[0]
    .trim()
  const errorLines = errorSection.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
  const coreError = (errorLines.length > 1 ? errorLines[errorLines.length - 1] : errorSection)
    ?.replace(/\s+/g, ' ')
    .trim() || ''
  if (!coreError) return '运行输出过长，已隐藏具体内容'
  if (coreError.length <= MAX_OBSERVATION_ERROR_LENGTH) return coreError
  return `${coreError.slice(0, MAX_OBSERVATION_ERROR_LENGTH).trimEnd()}…`
}

function describeObservation(event: Extract<AgentTraceEvent, { type: 'agent_observation' }>): string {
  if (!event.success) return summarizeObservationError(event.error)
  const summary = event.summary
  if (!summary) return `工具执行完成，耗时 ${event.duration_ms} ms`
  if (typeof summary.match_count === 'number') {
    return `知识库命中 ${summary.match_count} 个相关片段，耗时 ${event.duration_ms} ms`
  }
  const missingRate = typeof summary.missing_rate === 'number'
    ? `，缺失率 ${(summary.missing_rate * 100).toFixed(1)}%`
    : ''
  return `${summary.file_count ?? 0} 个文件，${summary.table_count ?? 0} 张表，${summary.row_count ?? 0} 行，${summary.column_count ?? 0} 列${missingRate}`
}

/** 收集目录路径，用于「全部展开 / 全部收起」 */
function collectDirPaths(nodes: ProjectFileNode[], acc: string[] = []): string[] {
  for (const node of nodes) {
    if (node.type !== 'dir') continue
    acc.push(node.path)
    if (node.children?.length) collectDirPaths(node.children, acc)
  }
  return acc
}

function filterFileTree(nodes: ProjectFileNode[], keyword: string): ProjectFileNode[] {
  if (!keyword) return nodes
  const lower = keyword.trim().toLowerCase()
  if (!lower) return nodes
  const walk = (list: ProjectFileNode[]): ProjectFileNode[] => {
    const result: ProjectFileNode[] = []
    for (const node of list) {
      const children = node.children?.length ? walk(node.children) : undefined
      const matched = node.name.toLowerCase().includes(lower)
        || node.path.toLowerCase().includes(lower)
      if (matched || (children && children.length)) {
        result.push({
          ...node,
          children: node.type === 'dir' ? (children ?? []) : undefined,
        })
      }
    }
    return result
  }
  return walk(nodes)
}

const ProjectFileTree = memo(function ProjectFileTree({
  nodes,
  depth = 0,
  expanded,
  onToggle,
  selectedPath,
  onSelect,
}: {
  nodes: ProjectFileNode[]
  depth?: number
  expanded: Set<string>
  onToggle: (path: string) => void
  selectedPath: string | null
  onSelect: (path: string) => void
}) {
  if (!nodes.length) return null
  return (
    <ul className="proj-file-tree" style={{ '--tree-depth': depth } as React.CSSProperties}>
      {nodes.map((node) => {
        if (node.type === 'dir') {
          const isOpen = expanded.has(node.path)
          return (
            <li key={`dir-${node.path}`} className="proj-file-dir">
              <button
                type="button"
                className="proj-file-row folder"
                onClick={() => onToggle(node.path)}
                aria-expanded={isOpen}
                title={node.path}
              >
                <svg
                  className={`proj-file-chevron ${isOpen ? 'is-open' : ''}`}
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  aria-hidden="true"
                >
                  <path d="M9 6l6 6-6 6" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
                <svg className="proj-file-icon folder" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
                  <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" />
                </svg>
                <span className="proj-file-name">{node.name}</span>
              </button>
              {isOpen ? (
                <ProjectFileTree
                  nodes={node.children ?? []}
                  depth={depth + 1}
                  expanded={expanded}
                  onToggle={onToggle}
                  selectedPath={selectedPath}
                  onSelect={onSelect}
                />
              ) : null}
            </li>
          )
        }
        const isSelected = selectedPath === node.path
        return (
          <li key={`file-${node.path}`}>
            <button
              type="button"
              className={`proj-file-row file${isSelected ? ' is-selected' : ''}`}
              onClick={() => onSelect(node.path)}
              title={node.path}
            >
              <span className="proj-file-chevron-spacer" aria-hidden="true" />
              <svg className="proj-file-icon file" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
                <path d="M7 3h7l5 5v13a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z" />
                <path d="M14 3v5h5" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              <span className="proj-file-name">{node.name}</span>
            </button>
          </li>
        )
      })}
    </ul>
  )
})

/** 项目文件浏览器：顶部按钮开关；收起时不占侧边栏 */
function ProjectFileExplorer({
  projectId,
  collapsed,
  onCollapse,
}: {
  projectId: string | null
  collapsed: boolean
  onCollapse: () => void
}) {
  const [tree, setTree] = useState<{ title: string; rootPath: string; nodes: ProjectFileNode[] } | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [keyword, setKeyword] = useState('')
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [selectedPath, setSelectedPath] = useState<string | null>(null)
  const [openingFolder, setOpeningFolder] = useState(false)
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    if (collapsed || !projectId) {
      setTree(null)
      setError('')
      return
    }
    let cancelled = false
    setLoading(true)
    setError('')
    void fetchProjectFileTree(projectId)
      .then((data) => {
        if (cancelled) return
        setTree({ title: data.title, rootPath: data.root_path, nodes: data.nodes })
        // 默认展开第一层，便于一眼看到项目结构
        const firstLevel = data.nodes
          .filter((node) => node.type === 'dir')
          .map((node) => node.path)
        setExpanded(new Set(firstLevel))
        setSelectedPath(null)
      })
      .catch((reason: unknown) => {
        if (cancelled) return
        setTree(null)
        setError(reason instanceof Error ? reason.message : '无法读取项目文件树')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [projectId, collapsed, reloadKey])

  const visibleNodes = useMemo(
    () => (tree ? filterFileTree(tree.nodes, keyword) : []),
    [tree, keyword],
  )

  function toggleDir(path: string) {
    setExpanded((current) => {
      const next = new Set(current)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })
  }

  function expandAll() {
    if (!tree) return
    setExpanded(new Set(collectDirPaths(tree.nodes)))
  }

  function collapseAll() {
    setExpanded(new Set())
  }

  async function handleOpenFolder() {
    if (!projectId || openingFolder) return
    setOpeningFolder(true)
    setError('')
    try {
      await openProjectFolder(projectId)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法打开项目文件夹')
    } finally {
      setOpeningFolder(false)
    }
  }

  return (
    <>
      <div className="rag-sources-head proj-files-head">
        <div>
          <h2>项目文件</h2>
          <p title={tree?.rootPath}>{tree?.title ?? '当前项目目录结构'}</p>
        </div>
        <div className="rag-sources-head-actions">
          <button
            type="button"
            className="proj-file-tool"
            onClick={() => setReloadKey((key) => key + 1)}
            disabled={loading || !projectId}
            title="刷新"
            aria-label="刷新文件树"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
              <path d="M20 12a8 8 0 1 1-2.3-5.7" strokeLinecap="round" />
              <path d="M20 4v5h-5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
          <button
            type="button"
            className="proj-file-tool"
            onClick={expandAll}
            disabled={!tree?.nodes.length}
            title="全部展开"
            aria-label="全部展开"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
              <path d="M7 10l5 5 5-5" strokeLinecap="round" strokeLinejoin="round" />
              <path d="M7 4h10" strokeLinecap="round" />
              <path d="M7 20h10" strokeLinecap="round" />
            </svg>
          </button>
          <button
            type="button"
            className="proj-file-tool"
            onClick={collapseAll}
            disabled={!expanded.size}
            title="全部收起"
            aria-label="全部收起"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
              <path d="M7 14l5-5 5 5" strokeLinecap="round" strokeLinejoin="round" />
              <path d="M7 4h10" strokeLinecap="round" />
              <path d="M7 20h10" strokeLinecap="round" />
            </svg>
          </button>
          <button
            type="button"
            className="proj-file-tool"
            onClick={() => void handleOpenFolder()}
            disabled={openingFolder || !projectId}
            title="在文件管理器中打开"
            aria-label="打开项目文件夹"
          >
            {openingFolder ? '…' : (
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" />
              </svg>
            )}
          </button>
          <button
            type="button"
            className="rag-sources-collapse"
            onClick={onCollapse}
            title="收起项目文件"
            aria-label="收起项目文件"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M9 18l6-6-6-6" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
        </div>
      </div>

      <div className="proj-file-search">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
          <circle cx="11" cy="11" r="6" />
          <path d="M16 16l4 4" strokeLinecap="round" />
        </svg>
        <input
          type="search"
          value={keyword}
          onChange={(event) => setKeyword(event.target.value)}
          placeholder="搜索文件…"
          aria-label="搜索项目文件"
          disabled={!tree}
        />
      </div>

      <div className="rag-sources-list proj-file-list">
        {error && <p className="rag-folder-error">{error}</p>}
        {!projectId ? (
          <p className="rag-sources-empty">请先在侧边栏选择项目文件夹。</p>
        ) : loading ? (
          <p className="rag-sources-empty">正在读取项目文件…</p>
        ) : !tree?.nodes.length ? (
          <p className="rag-sources-empty">项目文件夹为空。</p>
        ) : !visibleNodes.length ? (
          <p className="rag-sources-empty">没有匹配「{keyword}」的文件。</p>
        ) : (
          <ProjectFileTree
            nodes={visibleNodes}
            expanded={expanded}
            onToggle={toggleDir}
            selectedPath={selectedPath}
            onSelect={setSelectedPath}
          />
        )}
      </div>
    </>
  )
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
      <div className="agent-inline-steps">
        {trace.map((event, index) => (
          <div
            className={`agent-inline-step ${event.type}`}
            key={`${event.type}-${index}`}
          >
            {event.type === 'agent_started' ? (
              <p className="agent-inline-activity">
                {event.files.length ? `已查看 ${event.files.length} 个附件` : '已开始处理任务'}
              </p>
            ) : event.type === 'agent_action' ? (
              <>
                <p className="agent-inline-reasoning">{event.thought || '正在判断下一步操作。'}</p>
                <p className="agent-inline-activity">调用了工具 · {event.tool_name}</p>
                {event.code && (
                  <details className="agent-inline-code">
                    <summary>查看本轮 Python 代码</summary>
                    <pre><code>{event.code}</code></pre>
                  </details>
                )}
              </>
            ) : event.type === 'agent_command_approval_required' ? (
              <p className="agent-inline-activity">等待人工批准 · {event.command}</p>
            ) : event.type === 'agent_command_approval_decision' ? (
              <p className="agent-inline-activity">{event.approved ? '已批准命令' : '已拒绝命令'}</p>
            ) : event.type === 'agent_reasoning' ? (
              <p className="agent-inline-reasoning">{event.summary}</p>
            ) : (
              <>
                <p className={`agent-inline-activity ${event.success ? '' : 'failed'}`}>
                  {event.success ? '完成了工具' : '工具执行失败'} · {event.tool_name} · {describeObservation(event)}
                </p>
                {event.details && (
                  <details className="agent-inline-code">
                    <summary>查看命令输出或文件差异</summary>
                    <pre><code>{event.details}</code></pre>
                  </details>
                )}
              </>
            )}
          </div>
        ))}
        {running && <p className="agent-inline-activity is-running">正在继续分析…</p>}
      </div>
    </div>
  )
})

const KnowledgeSourcePanel = memo(function KnowledgeSourcePanel({
  sources,
}: {
  sources: KnowledgeSourceCitation[]
}) {
  if (!sources.length) return null
  const uniqueFiles = [...new Set(sources.map((item) => item.source_file))]
  return (
    <details className="rag-msg-sources">
      <summary title="展开或收起知识库来源">
        <span className="rag-msg-sources-label">来源</span>
        <span className="rag-msg-sources-count">
          {uniqueFiles.length === 1
            ? uniqueFiles[0]
            : `知识库命中 ${uniqueFiles.length} 个文档`}
        </span>
        <span className="rag-msg-sources-chevron" aria-hidden="true" />
      </summary>
      <ol className="rag-msg-sources-list">
        {sources.map((source, index) => (
          <li key={`${source.document_id ?? source.source_file}-${index}`}>
            <div className="rag-msg-sources-item-head">
              <span className="rag-msg-sources-file">{source.source_file}</span>
              {source.knowledge_base_id && (
                <span className="rag-msg-sources-kb" title={source.knowledge_base_id}>
                  知识库
                </span>
              )}
            </div>
            {source.excerpt && (
              <p className="rag-msg-sources-excerpt">{source.excerpt}</p>
            )}
          </li>
        ))}
      </ol>
    </details>
  )
})

const ChatMessage = memo(function ChatMessage({
  message,
  running = false,
}: {
  message: Message
  running?: boolean
}) {
  if (!message.content && !message.trace?.length) return null
  const sources = message.sources?.length
    ? message.sources
    : collectKnowledgeSources(message.trace)
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
            {sources.length > 0 && <KnowledgeSourcePanel sources={sources} />}
          </div>
        ) : (
          <div className="rag-msg-plain">{message.content}</div>
        )}
      </div>
    </div>
  )
})

type Props = {
  discussionId: string | null
  projectId: string | null
  onDiscussionId: (id: string) => void
  resumeRunId?: string | null
  onViewResult: (runId: string) => void
}

/** 将选中知识库收敛到真实存在的 ID：去重并过滤无效项。 */
function clampKnowledgeBaseIds(
  ids: string[],
  bases: KnowledgeBaseSummary[],
): string[] {
  if (!bases.length) return []
  const available = new Set(bases.map((base) => base.id))
  const unique = [...new Set(ids)].filter((id) => available.has(id))
  return unique
}

export default function NewChat({
  discussionId,
  projectId,
  onDiscussionId,
  resumeRunId,
  onViewResult,
}: Props) {
  const [mode, setMode] = useState<Mode>(resumeRunId ? 'modeling' : 'agent')
  const [draft, setDraft] = useState('')
  const [messages, setMessages] = useState<Message[]>([])
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [activeRunId, setActiveRunId] = useState<string | null>(null)
  const [isStopping, setIsStopping] = useState(false)
  const [files, setFiles] = useState<File[]>([])
  const [title, setTitle] = useState('新讨论')
  const [agentTrace, setAgentTrace] = useState<AgentTraceEvent[]>([])
  // 右侧项目文件面板默认收起，由顶部按钮开关
  const [sourcesCollapsed, setSourcesCollapsed] = useState(true)
  const [apiConfigs, setApiConfigs] = useState<ApiConfig[]>([])
  const [selectedConfigId, setSelectedConfigId] = useState<string>('')
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseSummary[]>([])
  // 不预选知识库，用户可以选择零个或多个
  const [knowledgeBaseIds, setKnowledgeBaseIds] = useState<string[]>([])
  const knowledgeBasesRef = useRef<KnowledgeBaseSummary[]>([])
  const fileInputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  // 人工批准弹窗：请求内容 + 结算回调（Promise 流内等待用户点按钮）
  const [approval, setApproval] = useState<ApprovalRequest | null>(null)
  const approvalResolveRef = useRef<((approved: boolean) => void) | null>(null)
  const streamControllerRef = useRef<AbortController | null>(null)
  const runningDiscussionRef = useRef<string | null>(null)

  useEffect(() => () => {
    streamControllerRef.current?.abort()
    if (approvalResolveRef.current) approvalResolveRef.current(false)
  }, [])

  function createStreamHandlers(controller: AbortController): AgentChatHandlers {
    const active = () => !controller.signal.aborted
    return {
      onStarted: (runId, _files, savedDiscussionId) => {
        if (!active()) return
        setActiveRunId(runId)
        if (savedDiscussionId) {
          runningDiscussionRef.current = savedDiscussionId
          onDiscussionId(savedDiscussionId)
        }
      },
      onTrace: (event) => {
        if (!active()) return
        setAgentTrace((current) => [...current, event])
        setMessages((current) => current.map((item, index) => {
          if (index !== current.length - 1) return item
          const nextTrace = [...(item.trace ?? []), event]
          return { ...item, trace: nextTrace, sources: collectKnowledgeSources(nextTrace) }
        }))
      },
      onApprovalRequired: async (event) => {
        if (!active()) return
        const approved = await new Promise<boolean>((resolve) => {
          approvalResolveRef.current = resolve
          setApproval({
            approval_id: event.approval_id,
            command: event.command,
            workspace_root: event.workspace_root,
            python_environment: event.python_environment,
          })
        })
        approvalResolveRef.current = null
        setApproval(null)
        if (active()) await decideAgentCommand(event.approval_id, approved)
      },
      onToken: (content) => {
        if (!active()) return
        setMessages((current) => current.map((item, index) => index === current.length - 1
          ? { ...item, content: `${item.content}${content}` }
          : item))
      },
      onDone: (_discussionId, _runId, _artifacts, sources, contextUsage) => {
        if (!active()) return
        setMessages((current) => current.map((item, index) => index === current.length - 1
          ? { ...item, sources: sources?.length ? sources : item.sources, contextUsage }
          : item))
        setActiveRunId(null)
        setIsSubmitting(false)
        setIsStopping(false)
        runningDiscussionRef.current = null
      },
      onCancelled: () => {
        if (!active()) return
        setMessages((current) => current.map((item, index) => index === current.length - 1
          ? { ...item, content: '任务已终止。' }
          : item))
        setActiveRunId(null)
        setIsSubmitting(false)
        setIsStopping(false)
        runningDiscussionRef.current = null
      },
    }
  }

  // 批准卡片出现在输入框上方时，保证消息区已滚到底、卡片可见
  useEffect(() => {
    if (!approval) return
    const el = listRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [approval])

  function settleApproval(approved: boolean) {
    const resolve = approvalResolveRef.current
    approvalResolveRef.current = null
    setApproval(null)
    if (resolve) resolve(approved)
  }

  function applyApiConfigs(configs: ApiConfig[]) {
    setApiConfigs(configs)
    setSelectedConfigId((current) =>
      configs.some((config) => config.id === current) ? current : (configs[0]?.id ?? ''),
    )
  }

  useEffect(() => {
    let cancelled = false
    applyApiConfigs(loadConfigs())
    void syncApiSettingsCache().then((synced) => {
      if (!synced || cancelled) return
      applyApiConfigs(loadConfigs())
    })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    void getKnowledgeBases().then((bases) => {
      knowledgeBasesRef.current = bases
      setKnowledgeBases(bases)
      // 目录变化时只移除已不存在的知识库
      setKnowledgeBaseIds((current) => clampKnowledgeBaseIds(current, bases))
    })
  }, [])

  useEffect(() => {
    if (discussionId && runningDiscussionRef.current === discussionId) return
    streamControllerRef.current?.abort()
    streamControllerRef.current = null
    runningDiscussionRef.current = null
    if (approvalResolveRef.current) approvalResolveRef.current(false)
    approvalResolveRef.current = null
    setApproval(null)
    setActiveRunId(null)
    setIsSubmitting(false)
    setIsStopping(false)
    if (!discussionId) {
      setMessages([])
      setTitle('新讨论')
      setAgentTrace([])
      return
    }
    let cancelled = false
    void fetchActiveAgentChatRun(discussionId)
      .then(async (activeRun) => ({
        activeRun,
        discussion: await fetchBrainstormDiscussion(discussionId),
      }))
      .then(({ discussion, activeRun }) => {
        if (cancelled) return
        const activeRunId = activeRun?.run_id
        if (activeRunId && discussion.messages.some((item) => item.metadata?.agent_run_id === activeRunId)) {
          activeRun = null
        }
        const savedMetadata = [...discussion.messages]
          .reverse()
          .find((message) => Array.isArray(message.metadata?.knowledge_base_ids) || message.metadata?.knowledge_base_id)
          ?.metadata
        const savedRoutes = savedMetadata?.knowledge_base_ids
          ?? (savedMetadata?.knowledge_base_id ? [savedMetadata.knowledge_base_id] : [])
        if (savedMetadata) {
          // 历史讨论可能残留 default 等已不存在的 ID，按当前目录过滤
          const bases = knowledgeBasesRef.current
          setKnowledgeBaseIds(bases.length ? clampKnowledgeBaseIds(savedRoutes, bases) : savedRoutes)
        }
        const latestAgentMessage = [...discussion.messages]
          .reverse()
          .find((message) => message.metadata?.mode === 'agent')
        setAgentTrace(latestAgentMessage?.metadata?.agent_trace ?? [])
        const savedMessages: Message[] = discussion.messages.map((m) => ({
            role: m.role,
            content: m.content,
            trace: m.metadata?.agent_trace,
            contextUsage: m.metadata?.context_usage,
            sources: m.sources?.length
              ? m.sources
              : m.metadata?.sources,
          }))
        setMessages(activeRun
          ? [...savedMessages, { role: 'user', content: activeRun.message }, { role: 'assistant', content: '' }]
          : savedMessages)
        setTitle(discussion.title)
        if (activeRun) {
          const controller = new AbortController()
          streamControllerRef.current = controller
          runningDiscussionRef.current = discussionId
          setActiveRunId(activeRun.run_id)
          setIsSubmitting(true)
          void reconnectAgentChatRun(activeRun.run_id, createStreamHandlers(controller), controller.signal)
            .catch((error) => {
              if (controller.signal.aborted) return
              setMessages((current) => current.map((item, index) => index === current.length - 1
                ? { ...item, content: error instanceof Error ? error.message : '任务连接中断。' }
                : item))
              setIsSubmitting(false)
              setActiveRunId(null)
            })
        }
      })
      .catch((error) => {
        if (cancelled) return
        setMessages([
          {
            role: 'assistant',
            content: error instanceof Error ? error.message : '加载讨论记录失败。',
          },
        ])
      })
    return () => { cancelled = true }
  }, [discussionId])

  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages])

  function handlePaste(event: ClipboardEvent<HTMLTextAreaElement>) {
    if (isSubmitting) return
    const timestamp = Date.now()
    const pastedImages = Array.from(event.clipboardData.items)
      .filter((item) => item.kind === 'file' && item.type.startsWith('image/'))
      .map((item) => item.getAsFile())
      .filter((file): file is File => file !== null)
      .map((file, index) => {
        const extension = CLIPBOARD_IMAGE_EXTENSIONS[file.type] ?? 'png'
        return new File(
          [file],
          `clipboard-image-${timestamp}-${index + 1}.${extension}`,
          { type: file.type || 'image/png', lastModified: timestamp },
        )
      })
    if (!pastedImages.length) return
    event.preventDefault()
    setFiles((current) => [...current, ...pastedImages])
  }

  async function submit() {
    const message = draft.trim()
    if ((!message && files.length === 0) || isSubmitting) return
    setDraft('')
    setIsSubmitting(true)
    setIsStopping(false)
    const attachedFiles = files
    setFiles([])
    if (fileInputRef.current) fileInputRef.current.value = ''
    setMessages((current) => [
      ...current,
      { role: 'user', content: message },
      { role: 'assistant', content: '' },
    ])
    const controller = new AbortController()
    streamControllerRef.current = controller
    try {
      setAgentTrace([])
      await streamDataAgentMessage(
        message,
        discussionId,
        title,
        attachedFiles,
        createStreamHandlers(controller),
        selectedConfigId || null,
        knowledgeBaseIds,
        projectId,
        controller.signal,
      )
    } catch (error) {
      if (controller.signal.aborted) return
      setMessages((current) => current.map(
        (item, index) => index === current.length - 1
          ? {
              ...item,
              role: 'assistant',
              content: error instanceof Error ? error.message : 'Agent 暂不可用。',
            }
          : item,
      ))
    } finally {
      if (!controller.signal.aborted) {
        setIsSubmitting(false)
        setActiveRunId(null)
      }
    }
  }

  async function stopActiveRun() {
    if (!activeRunId || isStopping) return
    setIsStopping(true)
    try {
      await cancelAgentChatRun(activeRunId)
    } catch (error) {
      setIsStopping(false)
      setMessages((current) => current.map((item, index) => index === current.length - 1
        ? { ...item, content: `${item.content}\n\n${error instanceof Error ? error.message : '终止任务失败。'}` }
        : item))
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
          <button type="button" className="rag-btn" onClick={() => setMode('agent')}>
            ← 返回数分 Agent
          </button>
          <span className="new-chat-mode-hint">建模任务 · 上传资料后启动完整建模流程</span>
        </div>
        <ChatModeling
          key={resumeRunId ?? 'modeling'}
          resumeRunId={resumeRunId}
          onViewResult={onViewResult}
          knowledgeBases={knowledgeBases}
          knowledgeBaseIds={knowledgeBaseIds}
          onKnowledgeBaseIdsChange={setKnowledgeBaseIds}
          apiConfig={apiConfigs.find((config) => config.id === selectedConfigId) ?? null}
        />
      </div>
    )
  }

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
        </div>
        <div className="rag-top-actions">
          <button
            type="button"
            className={`rag-top-tool${sourcesCollapsed ? '' : ' is-active'}`}
            onClick={() => setSourcesCollapsed((open) => !open)}
            title={sourcesCollapsed ? '打开项目文件' : '收起项目文件'}
            aria-label={sourcesCollapsed ? '打开项目文件' : '收起项目文件'}
            aria-pressed={!sourcesCollapsed}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
              <rect x="3.5" y="4.5" width="17" height="15" rx="2.5" />
              <path d="M9.5 4.5v15" strokeLinecap="round" />
            </svg>
          </button>
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
                  running={isSubmitting && index === messages.length - 1}
                />
              ))}
              {messages.length === 0 && (
                <div className="agent-empty-state">
                  <img className="agent-empty-logo" src="/logo.png" alt="" aria-hidden="true" />
                  <h2>今天想分析什么？</h2>
                </div>
              )}
              {isSubmitting
                && messages[messages.length - 1]?.content === ''
                && agentTrace.length === 0 && (
                <div className="rag-msg assistant">
                  <span className="rag-avatar">M</span>
                  <div className="rag-msg-body">
                    <span className="rag-typing" aria-label="Agent 运行中">
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
            {approval ? (
              <CommandApprovalDialog
                command={approval.command}
                workspaceRoot={approval.workspace_root}
                pythonEnvironment={approval.python_environment}
                onApprove={() => settleApproval(true)}
                onReject={() => settleApproval(false)}
              />
            ) : null}
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
                onPaste={handlePaste}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && !event.shiftKey) {
                    event.preventDefault()
                    void submit()
                  }
                }}
                placeholder="直接提问，或上传/粘贴数据、文档和图片让 Agent 自主分析…"
                rows={2}
              />
              <div className="rag-composer-bar">
                <details className="rag-kb-picker">
                  <summary title="选择本轮检索使用的知识库">
                    {knowledgeBaseIds.length === 1
                      ? knowledgeBases.find((base) => base.id === knowledgeBaseIds[0])?.name ?? '选择知识库'
                      : knowledgeBaseIds.length > 1
                        ? `已选 ${knowledgeBaseIds.length} 个知识库`
                        : '不使用知识库'}
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
                              return current.filter((id) => id !== base.id)
                            }
                            return [...current, base.id]
                          })}
                        />
                        <span>{base.name}</span>
                        <small>{base.document_count}</small>
                      </label>
                    ))}
                  </div>
                </details>
                <button
                  type="button"
                  className="rag-btn"
                  onClick={() => fileInputRef.current?.click()}
                  disabled={isSubmitting}
                >
                  添加附件{files.length ? ` · ${files.length}` : ''}
                </button>
                <input
                  ref={fileInputRef}
                  hidden
                  type="file"
                  multiple
                  accept=".csv,.xlsx,.xls,.json,.jsonl,.ndjson,.pdf,.docx,.md,.markdown,.py,.png,.jpg,.jpeg,.webp,.gif,.bmp,.tif,.tiff"
                  onChange={(event) => setFiles(Array.from(event.target.files ?? []))}
                />
                <ContextUsageRing usage={[...messages].reverse().find((item) => item.contextUsage)?.contextUsage} />
                {apiConfigs.length > 0 ? (() => {
                  const activeModelConfig = apiConfigs.find((config) => config.id === selectedConfigId)
                    ?? apiConfigs[0]
                  const activeModelLabel = activeModelConfig.model || activeModelConfig.name || '未命名模型'
                  return (
                    <details className="rag-model-picker" title="选择对话使用的模型（来自 API 设置）">
                      <summary className={isSubmitting ? 'is-disabled' : undefined} aria-label="选择模型">
                        <span className="rag-model-badge" aria-hidden="true">
                          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
                            <path d="M12 3l1.8 4.8L19 9.6l-4.2 3.2L16.2 18 12 15.2 7.8 18l1.4-5.2L5 9.6l5.2-1.8L12 3z" strokeLinejoin="round" />
                          </svg>
                        </span>
                        <span className="rag-model-name">{activeModelLabel}</span>
                        <span className="rag-kb-chevron" aria-hidden="true" />
                      </summary>
                      <div className="rag-model-menu" role="listbox" aria-label="可用模型">
                        <div className="rag-model-menu-head">选择模型</div>
                        {apiConfigs.map((config) => {
                          const label = config.model || config.name || '未命名模型'
                          const isActive = config.id === activeModelConfig.id
                          return (
                            <button
                              key={config.id}
                              type="button"
                              role="option"
                              aria-selected={isActive}
                              className={`rag-model-option${isActive ? ' is-active' : ''}`}
                              disabled={isSubmitting}
                              onClick={(event) => {
                                setSelectedConfigId(config.id)
                                const details = event.currentTarget.closest('details')
                                if (details instanceof HTMLDetailsElement) details.open = false
                              }}
                            >
                              <span className="rag-model-option-main">
                                <span className="rag-model-option-name">{label}</span>
                                {config.reasoningEffort && config.reasoningEffort !== 'off' ? (
                                  <span className="rag-model-effort">思考 {config.reasoningEffort}</span>
                                ) : null}
                              </span>
                              {isActive ? (
                                <svg className="rag-model-check" width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                                  <path d="M5 12.5l4.5 4.5L19 7" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
                                </svg>
                              ) : null}
                            </button>
                          )
                        })}
                      </div>
                    </details>
                  )
                })() : (
                  <span className="rag-model-empty" title="尚未在 API 管理中配置模型">
                    未配置模型
                  </span>
                )}
                <button
                  type="button"
                  className={`rag-send${isSubmitting ? ' rag-stop' : ''}`}
                  onClick={() => isSubmitting ? void stopActiveRun() : void submit()}
                  disabled={isSubmitting ? !activeRunId || isStopping : (!draft.trim() && files.length === 0)}
                  aria-label={isSubmitting ? (isStopping ? '正在终止任务' : '终止任务') : '发送'}
                  title={isSubmitting ? (isStopping ? '正在等待当前工具结束' : '终止当前任务') : '发送'}
                >
                  {isSubmitting ? <span className="rag-stop-icon" aria-hidden="true" /> : (
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                      <path d="M5 12h14M13 6l6 6-6 6" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                  )}
                </button>
              </div>
            </div>
          </div>
        </section>

        <aside
          className={`rag-sources ${sourcesCollapsed ? 'is-collapsed' : ''}`}
          aria-label="项目文件"
          aria-hidden={sourcesCollapsed}
        >
          <ProjectFileExplorer
            projectId={projectId}
            collapsed={sourcesCollapsed}
            onCollapse={() => setSourcesCollapsed(true)}
          />
        </aside>
      </div>
    </div>
  )
}

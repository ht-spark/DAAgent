import { memo, useEffect, useRef, useState, type ClipboardEvent } from 'react'
import { createPortal } from 'react-dom'

import {
  decideAgentCommand,
  fetchBrainstormDiscussion,
  getKnowledgeBases,
  openAgentArtifactFolder,
  renameBrainstormDiscussion,
  streamDataAgentMessage,
  syncApiSettingsCache,
  type AgentArtifact,
  type AgentTraceEvent,
  type KnowledgeBaseSummary,
  type KnowledgeSourceCitation,
} from '../api'
import { loadConfigs, type ApiConfig } from '../apiConfigs'
import MarkdownContent from '../components/MarkdownContent'
import ChatModeling from './ChatModeling'

type Mode = 'agent' | 'modeling'

type Message = {
  role: 'assistant' | 'user'
  content: string
  trace?: AgentTraceEvent[]
  /** 本轮检索命中的知识库来源（可收起标注） */
  sources?: KnowledgeSourceCitation[]
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
  const coreError = (errorLines.length > 1 ? errorLines.at(-1) : errorSection)
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

function formatArtifactSize(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}

function artifactType(name: string): string {
  const extension = name.split('.').pop()
  return extension && extension !== name ? extension.toUpperCase() : 'FILE'
}

/** 产物目录树节点：按 path 分段构建，便于像资源管理器一样浏览。 */
type ArtifactTreeNode = {
  type: 'folder' | 'file'
  name: string
  path: string
  children?: ArtifactTreeNode[]
  artifact?: AgentArtifact
}

function buildArtifactTree(artifacts: AgentArtifact[]): ArtifactTreeNode[] {
  const root: ArtifactTreeNode[] = []
  const folderIndex = new Map<string, ArtifactTreeNode>()

  const ensureFolder = (segments: string[]): ArtifactTreeNode[] => {
    let siblings = root
    let currentPath = ''
    for (const segment of segments) {
      currentPath = currentPath ? `${currentPath}/${segment}` : segment
      let node = folderIndex.get(currentPath)
      if (!node) {
        node = { type: 'folder', name: segment, path: currentPath, children: [] }
        folderIndex.set(currentPath, node)
        siblings.push(node)
      }
      siblings = node.children!
    }
    return siblings
  }

  for (const artifact of artifacts) {
    const parts = artifact.path.split('/').filter(Boolean)
    if (!parts.length) continue
    const fileName = parts[parts.length - 1]
    const folderSegments = parts.slice(0, -1)
    const siblings = ensureFolder(folderSegments)
    siblings.push({
      type: 'file',
      name: fileName,
      path: artifact.path,
      artifact,
    })
  }

  const sortNodes = (nodes: ArtifactTreeNode[]) => {
    nodes.sort((a, b) => {
      if (a.type !== b.type) return a.type === 'folder' ? -1 : 1
      return a.name.localeCompare(b.name, 'zh-CN')
    })
    for (const node of nodes) {
      if (node.children) sortNodes(node.children)
    }
  }
  sortNodes(root)
  return root
}

const ArtifactTree = memo(function ArtifactTree({
  nodes,
  depth = 0,
  onPreview,
}: {
  nodes: ArtifactTreeNode[]
  depth?: number
  onPreview: (artifact: AgentArtifact) => void
}) {
  const [collapsedFolders, setCollapsedFolders] = useState<Set<string>>(new Set())

  function toggleFolder(path: string) {
    setCollapsedFolders((current) => {
      const next = new Set(current)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })
  }

  return (
    <ul className="artifact-tree" style={{ '--tree-depth': depth } as React.CSSProperties}>
      {nodes.map((node) => {
        if (node.type === 'folder') {
          const isOpen = !collapsedFolders.has(node.path)
          return (
            <li key={`folder-${node.path}`} className="artifact-tree-folder">
              <button
                type="button"
                className="artifact-tree-row folder"
                onClick={() => toggleFolder(node.path)}
                aria-expanded={isOpen}
              >
                <svg
                  className={`artifact-tree-chevron ${isOpen ? 'is-open' : ''}`}
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  aria-hidden="true"
                >
                  <path d="M9 6l6 6-6 6" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
                <svg className="artifact-tree-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
                  <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" />
                </svg>
                <span className="artifact-tree-name">{node.name}</span>
              </button>
              {isOpen && node.children?.length ? (
                <ArtifactTree nodes={node.children} depth={depth + 1} onPreview={onPreview} />
              ) : null}
            </li>
          )
        }
        const artifact = node.artifact
        if (!artifact) return null
        return (
          <li key={`file-${artifact.run_id}-${node.path}`}>
            <button
              type="button"
              className="artifact-tree-row file"
              onClick={() => onPreview(artifact)}
              title={`${node.path} · ${formatArtifactSize(artifact.size_bytes)}`}
            >
              <svg className="artifact-tree-icon file" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
                <path d="M7 3h7l5 5v13a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z" />
                <path d="M14 3v5h5" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              <span className="artifact-tree-name">{node.name}</span>
              <small className="artifact-tree-size">{formatArtifactSize(artifact.size_bytes)}</small>
            </button>
          </li>
        )
      })}
    </ul>
  )
})

type ArtifactPreviewKind = 'image' | 'pdf' | 'markdown' | 'code' | 'docx' | 'xlsx' | 'unsupported'

type ArtifactPreviewData = {
  kind: 'docx'
  paragraphs: string[]
  tables: string[][][]
  truncated: boolean
} | {
  kind: 'xlsx'
  sheets: Array<{ name: string; rows: Array<Array<string | number | boolean | null>> }>
  truncated: boolean
}

const IMAGE_PREVIEW_EXTENSIONS = new Set(['bmp', 'gif', 'jpeg', 'jpg', 'png', 'svg', 'tif', 'tiff', 'webp'])
const CODE_PREVIEW_EXTENSIONS = new Set([
  'bat', 'c', 'cc', 'cpp', 'cs', 'css', 'csv', 'go', 'h', 'hpp', 'htm', 'html',
  'ini', 'ipynb', 'java', 'js', 'json', 'jsonl', 'jsx', 'log', 'ndjson', 'ps1',
  'py', 'r', 'rmd', 'rs', 'sh', 'sql', 'toml', 'ts', 'tsx', 'txt', 'xml', 'yaml', 'yml',
])

function artifactExtension(name: string): string {
  const extension = name.split('.').pop()
  return extension && extension !== name ? extension.toLowerCase() : ''
}

function artifactPreviewKind(name: string): ArtifactPreviewKind {
  const extension = artifactExtension(name)
  if (IMAGE_PREVIEW_EXTENSIONS.has(extension)) return 'image'
  if (extension === 'pdf') return 'pdf'
  if (extension === 'md' || extension === 'markdown') return 'markdown'
  if (extension === 'docx') return 'docx'
  if (extension === 'xlsx') return 'xlsx'
  if (CODE_PREVIEW_EXTENSIONS.has(extension)) return 'code'
  return 'unsupported'
}

function artifactUrl(artifact: AgentArtifact, action: 'files' | 'previews'): string {
  const path = artifact.path.split('/').map(encodeURIComponent).join('/')
  return artifact.discussion_id
    ? `/api/agent/discussions/${encodeURIComponent(artifact.discussion_id)}/${action}/${path}`
    : `/api/agent/runs/${artifact.run_id}/${action}/${path}`
}

function resolveArtifactAssetUrl(artifact: AgentArtifact, src: string): string {
  if (!src) return src
  if (
    src.startsWith('data:') ||
    src.startsWith('blob:') ||
    src.startsWith('/') ||
    /^[a-z][a-z0-9+.-]*:/i.test(src)
  ) {
    return src
  }

  const baseDir = artifact.path.includes('/')
    ? artifact.path.slice(0, artifact.path.lastIndexOf('/'))
    : ''
  const segments: string[] = []
  for (const segment of `${baseDir}/${src}`.split('/')) {
    if (!segment || segment === '.') continue
    if (segment === '..') {
      segments.pop()
      continue
    }
    segments.push(segment)
  }
  return artifactUrl({ ...artifact, path: segments.join('/') }, 'files')
}

const ArtifactPreview = memo(function ArtifactPreview({
  artifact,
  onClose,
  onOpenFolder,
}: {
  artifact: AgentArtifact
  onClose: () => void
  onOpenFolder?: () => void
}) {
  const kind = artifactPreviewKind(artifact.name)
  const previewUrl = artifactUrl(artifact, 'previews')
  const [text, setText] = useState('')
  const [data, setData] = useState<ArtifactPreviewData | null>(null)
  const [activeSheet, setActiveSheet] = useState(0)
  const [loading, setLoading] = useState(!['image', 'pdf', 'unsupported'].includes(kind))
  const [error, setError] = useState('')
  const [truncated, setTruncated] = useState(false)

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  useEffect(() => {
    if (kind === 'image' || kind === 'pdf' || kind === 'unsupported') return undefined
    const controller = new AbortController()
    setLoading(true)
    setError('')
    setText('')
    setData(null)
    setActiveSheet(0)
    void fetch(previewUrl, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) {
          const payload = await response.json().catch(() => null) as { detail?: string } | null
          throw new Error(payload?.detail || '预览加载失败')
        }
        if (kind === 'docx' || kind === 'xlsx') {
          setData(await response.json() as ArtifactPreviewData)
        } else {
          setText(await response.text())
          setTruncated(response.headers.get('X-Preview-Truncated') === 'true')
        }
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === 'AbortError') return
        setError(reason instanceof Error ? reason.message : '预览加载失败')
      })
      .finally(() => setLoading(false))
    return () => controller.abort()
  }, [kind, previewUrl])

  const sheet = data?.kind === 'xlsx' ? data.sheets[activeSheet] : null
  const pathSegments = artifact.path.split('/').filter(Boolean)

  return createPortal(
    <div className="artifact-preview-mask" onMouseDown={onClose}>
      <section
        className="artifact-preview-dialog"
        role="dialog"
        aria-modal="true"
        aria-label={`${artifact.name} 预览`}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="artifact-preview-head">
          <div>
            <nav className="artifact-preview-crumb" aria-label="文件路径">
              {pathSegments.map((segment, index) => (
                <span key={`${segment}-${index}`}>
                  {index > 0 && <i aria-hidden="true">/</i>}
                  {segment}
                </span>
              ))}
            </nav>
            <h2>{artifact.name}</h2>
          </div>
          <nav aria-label="预览操作">
            {onOpenFolder && (
              <button type="button" onClick={onOpenFolder}>打开文件夹</button>
            )}
            <button type="button" onClick={onClose}>关闭</button>
          </nav>
        </header>
        <div className={`artifact-preview-body kind-${kind}`}>
          {loading && <p className="artifact-preview-state">正在加载预览…</p>}
          {error && <p className="artifact-preview-state error">{error}</p>}
          {!loading && !error && kind === 'image' && <img src={previewUrl} alt={artifact.name} />}
          {!loading && !error && kind === 'pdf' && <iframe src={previewUrl} title={artifact.name} />}
          {!loading && !error && kind === 'markdown' && (
            <article className="artifact-preview-markdown">
              <MarkdownContent
                content={text}
                transformUrl={(src) => resolveArtifactAssetUrl(artifact, src)}
              />
            </article>
          )}
          {!loading && !error && kind === 'code' && <pre><code>{text}</code></pre>}
          {!loading && !error && data?.kind === 'docx' && (
            <article className="artifact-preview-document">
              {data.paragraphs.map((paragraph, index) => <p key={`${index}-${paragraph.slice(0, 24)}`}>{paragraph}</p>)}
              {data.tables.map((table, tableIndex) => (
                <div className="artifact-preview-table-wrap" key={`table-${tableIndex}`}>
                  <table><tbody>{table.map((row, rowIndex) => (
                    <tr key={`row-${rowIndex}`}>{row.map((cell, cellIndex) => (
                      <td key={`cell-${cellIndex}`}>{cell}</td>
                    ))}</tr>
                  ))}</tbody></table>
                </div>
              ))}
            </article>
          )}
          {!loading && !error && data?.kind === 'xlsx' && (
            <div className="artifact-preview-workbook">
              <div className="artifact-preview-tabs" role="tablist" aria-label="工作表">
                {data.sheets.map((item, index) => (
                  <button
                    type="button"
                    role="tab"
                    aria-selected={index === activeSheet}
                    className={index === activeSheet ? 'active' : ''}
                    key={item.name}
                    onClick={() => setActiveSheet(index)}
                  >
                    {item.name}
                  </button>
                ))}
              </div>
              {sheet && (
                <div className="artifact-preview-table-wrap">
                  <table><tbody>{sheet.rows.map((row, rowIndex) => (
                    <tr key={`row-${rowIndex}`}>{row.map((cell, cellIndex) => (
                      <td key={`cell-${cellIndex}`}>{cell === null ? '' : String(cell)}</td>
                    ))}</tr>
                  ))}</tbody></table>
                </div>
              )}
            </div>
          )}
          {!loading && !error && kind === 'unsupported' && (
            <p className="artifact-preview-state">该文件暂不支持在线预览，请下载后查看。</p>
          )}
          {(truncated || data?.truncated) && <p className="artifact-preview-note">文件较大，当前仅展示部分内容。</p>}
        </div>
      </section>
    </div>,
    document.body,
  )
})

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
  const [files, setFiles] = useState<File[]>([])
  const [title, setTitle] = useState('新讨论')
  const [agentTrace, setAgentTrace] = useState<AgentTraceEvent[]>([])
  const [agentArtifacts, setAgentArtifacts] = useState<AgentArtifact[]>([])
  const [previewArtifact, setPreviewArtifact] = useState<AgentArtifact | null>(null)
  const [openingArtifactFolder, setOpeningArtifactFolder] = useState(false)
  const [artifactFolderError, setArtifactFolderError] = useState('')
  // 进入新建任务时右侧「输出产物」默认收起，有产物后用户可手动展开
  const [sourcesCollapsed, setSourcesCollapsed] = useState(true)
  const [apiConfigs, setApiConfigs] = useState<ApiConfig[]>([])
  const [selectedConfigId, setSelectedConfigId] = useState<string>('')
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseSummary[]>([])
  // 不预选知识库，用户可以选择零个或多个
  const [knowledgeBaseIds, setKnowledgeBaseIds] = useState<string[]>([])
  const knowledgeBasesRef = useRef<KnowledgeBaseSummary[]>([])
  const fileInputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)

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
    setPreviewArtifact(null)
    setArtifactFolderError('')
    if (!discussionId) {
      setMessages([])
      setTitle('新讨论')
      setAgentTrace([])
      setAgentArtifacts([])
      return
    }
    void fetchBrainstormDiscussion(discussionId)
      .then((discussion) => {
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
        const artifactsByPath = new Map<string, AgentArtifact>()
        discussion.messages.forEach((message) => {
          message.metadata?.agent_artifacts?.forEach((artifact) => {
            artifactsByPath.set(artifact.path, artifact)
          })
        })
        setAgentArtifacts([...artifactsByPath.values()])
        setMessages(
          discussion.messages.map((m) => ({
            role: m.role,
            content: m.content,
            trace: m.metadata?.agent_trace,
            sources: m.sources?.length
              ? m.sources
              : m.metadata?.sources,
          })),
        )
        setTitle(discussion.title)
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
    const attachedFiles = files
    setFiles([])
    if (fileInputRef.current) fileInputRef.current.value = ''
    setMessages((current) => [
      ...current,
      { role: 'user', content: message },
      { role: 'assistant', content: '' },
    ])
    try {
      setAgentTrace([])
      await streamDataAgentMessage(
        message,
        discussionId,
        title,
        attachedFiles,
        {
          onStarted: () => undefined,
          onTrace: (event) => {
            setAgentTrace((current) => [...current, event])
            setMessages((current) => current.map(
              (item, index) => {
                if (index !== current.length - 1) return item
                const nextTrace = [...(item.trace ?? []), event]
                return {
                  ...item,
                  trace: nextTrace,
                  sources: collectKnowledgeSources(nextTrace),
                }
              },
            ))
          },
          onApprovalRequired: async (event) => {
            const environment = event.python_environment
              ? `\n目标环境：${event.python_environment}` : ''
            const approved = window.confirm(
              `Agent 请求运行项目命令。请检查后决定是否批准：\n\n${event.command}\n\n项目：${event.workspace_root}${environment}\n\n此命令可能安装依赖或执行项目代码。`,
            )
            await decideAgentCommand(event.approval_id, approved)
          },
          onToken: (content) => {
            setMessages((current) => current.map(
              (item, index) => index === current.length - 1
                ? { ...item, content: `${item.content}${content}` }
                : item,
            ))
          },
          onDone: (savedDiscussionId, _runId, artifacts, sources) => {
            setAgentArtifacts((current) => [
              ...current.filter(
                (item) => !artifacts.some((artifact) => artifact.path === item.path),
              ),
              ...artifacts,
            ])
            if (sources?.length) {
              setMessages((current) => current.map(
                (item, index) => index === current.length - 1
                  ? { ...item, sources }
                  : item,
              ))
            }
            onDiscussionId(savedDiscussionId)
          },
        },
        selectedConfigId || null,
        knowledgeBaseIds,
        projectId,
      )
    } catch (error) {
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
      setIsSubmitting(false)
    }
  }

  async function saveTitle() {
    const normalizedTitle = title.trim() || '新讨论'
    setTitle(normalizedTitle)
    if (discussionId) await renameBrainstormDiscussion(discussionId, normalizedTitle)
  }

  async function openArtifactFolder() {
    const targetDiscussionId = discussionId
      ?? agentArtifacts.find((artifact) => artifact.discussion_id)?.discussion_id
    if (!targetDiscussionId || openingArtifactFolder) return
    setOpeningArtifactFolder(true)
    setArtifactFolderError('')
    try {
      await openAgentArtifactFolder(targetDiscussionId)
    } catch (error) {
      setArtifactFolderError(error instanceof Error ? error.message : '无法打开产物文件夹')
    } finally {
      setOpeningArtifactFolder(false)
    }
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
          aria-label="输出产物"
        >
          {sourcesCollapsed ? (
            <button
              type="button"
              className="rag-sources-expand"
              onClick={() => setSourcesCollapsed(false)}
              title="展开输出产物"
            >
              <span className="rag-sources-expand-text">
                输出产物
              </span>
              {agentArtifacts.length > 0 && (
                <span className="rag-sources-expand-badge">
                  {agentArtifacts.length}
                </span>
              )}
            </button>
          ) : (
            <>
              <div className="rag-sources-head">
                <div>
                  <h2>输出产物</h2>
                  <p>项目文件夹中的产物，点击即可预览</p>
                </div>
                <div className="rag-sources-head-actions">
                  <button
                    type="button"
                    className="rag-open-folder"
                    onClick={() => void openArtifactFolder()}
                    disabled={openingArtifactFolder || (!discussionId && !agentArtifacts.some((artifact) => artifact.discussion_id))}
                    title="在文件管理器中打开产物文件夹"
                  >
                    {openingArtifactFolder ? '打开中…' : '打开文件夹'}
                  </button>
                  <button
                    type="button"
                    className="rag-sources-collapse"
                    onClick={() => setSourcesCollapsed(true)}
                    title="收起输出产物"
                    aria-label="收起输出产物"
                  >
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                      <path d="M9 18l6-6-6-6" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                  </button>
                </div>
              </div>
              <div className="rag-sources-list">
                {artifactFolderError && (
                  <p className="rag-folder-error">{artifactFolderError}</p>
                )}
                {agentArtifacts.length === 0 ? (
                  <p className="rag-sources-empty">
                    {isSubmitting ? 'Agent 正在处理，生成的文件会出现在这里。' : '当前讨论还没有生成产物文件。'}
                  </p>
                ) : (
                  <ArtifactTree
                    nodes={buildArtifactTree(agentArtifacts)}
                    onPreview={setPreviewArtifact}
                  />
                )}
              </div>
            </>
          )}
        </aside>
      </div>
      {previewArtifact && (
        <ArtifactPreview
          artifact={previewArtifact}
          onClose={() => setPreviewArtifact(null)}
          onOpenFolder={() => void openArtifactFolder()}
        />
      )}
    </div>
  )
}

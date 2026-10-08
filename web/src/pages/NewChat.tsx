import { memo, useEffect, useMemo, useRef, useState, type ClipboardEvent } from 'react'

import {
  cancelAgentChatRun,
  decideAgentCommand,
  fetchActiveAgentChatRun,
  fetchBrainstormDiscussion,
  fetchProjectFilePreview,
  fetchProjectFileTree,
  getKnowledgeBases,
  importSkill,
  listSkills,
  openProjectFolder,
  projectFilePreviewUrl,
  renameBrainstormDiscussion,
  reconnectAgentChatRun,
  streamDataAgentMessage,
  syncApiSettingsCache,
  updateBrainstormDiscussionModel,
  type AgentTraceEvent,
  type AgentChatHandlers,
  type ContextUsage,
  type KnowledgeBaseSummary,
  type KnowledgeSourceCitation,
  type ProjectFilePreview,
  type ProjectFileNode,
  type SkillInfo,
} from '../api'
import { loadConfigs, type ApiConfig } from '../apiConfigs'
import type { TerminalType } from '../settings'
import {
  TODO_STATUS_LABELS,
  countTodoStates,
  deriveTodoStates,
  latestAgentPlan,
  summarizeTodoObjective,
  type AgentPlan,
  type TodoStateMap,
  type TodoStatus,
} from '../agentTasks'
import AgentTrace from '../components/AgentTrace'
import CommandApprovalDialog from '../components/CommandApprovalDialog'
import MarkdownContent from '../components/MarkdownContent'
import ChatModeling from './ChatModeling'

type Mode = 'agent' | 'modeling'

const PROJECT_SIDEBAR_DEFAULT_WIDTH = 320
const PROJECT_SIDEBAR_MIN_WIDTH = 280
const PROJECT_SIDEBAR_MAX_WIDTH = 720
const PROJECT_SIDEBAR_MIN_CHAT_WIDTH = 480
const PROJECT_SIDEBAR_WIDTH_STORAGE_KEY = 'daagent-project-sidebar-width'

function clampProjectSidebarWidth(width: number, containerWidth?: number): number {
  const availableWidth = containerWidth ?? (
    typeof window === 'undefined' ? PROJECT_SIDEBAR_DEFAULT_WIDTH : window.innerWidth
  )
  const responsiveMax = Math.max(
    PROJECT_SIDEBAR_MIN_WIDTH,
    Math.min(PROJECT_SIDEBAR_MAX_WIDTH, availableWidth - PROJECT_SIDEBAR_MIN_CHAT_WIDTH),
  )
  return Math.round(Math.min(responsiveMax, Math.max(PROJECT_SIDEBAR_MIN_WIDTH, width)))
}

function loadProjectSidebarWidth(): number {
  if (typeof window === 'undefined') return PROJECT_SIDEBAR_DEFAULT_WIDTH
  try {
    const savedWidth = Number(window.localStorage.getItem(PROJECT_SIDEBAR_WIDTH_STORAGE_KEY))
    if (Number.isFinite(savedWidth) && savedWidth > 0) {
      return clampProjectSidebarWidth(savedWidth)
    }
  } catch {
    // 浏览器禁用本地存储时使用默认宽度。
  }
  return PROJECT_SIDEBAR_DEFAULT_WIDTH
}

/** 待人工批准的终端命令请求（对话流内弹窗，不再使用浏览器 confirm）。 */
type ApprovalRequest = {
  approval_id: string
  command: string
  workspace_root: string
  shell: TerminalType
  timeout_seconds?: number | null
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
        style={{ background: `conic-gradient(var(--rag-text) 0 ${percent}%, var(--rag-border-strong) ${percent}% 100%)` }}
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

/** 技能头像取色盘：与稿纸暖色主题协调的六种柔和底色 */
const SKILL_AVATAR_COLORS = ['#5b6db4', '#4f9a90', '#b8892d', '#a45a78', '#6c8a5e', '#8c6bae']

/** 取技能名首字符作为头像字母（英文名统一转大写） */
function skillAvatarLabel(name: string, fallback: string): string {
  const source = name.trim() || fallback.trim()
  return source ? source.slice(0, 1).toUpperCase() : '·'
}

/** 按技能 ID 稳定取色，保证同一技能每次渲染得到相同的头像颜色 */
function skillAvatarColor(skillId: string): string {
  let hash = 0
  for (const char of skillId) hash = (hash * 31 + (char.codePointAt(0) ?? 0)) % 9973
  return SKILL_AVATAR_COLORS[hash % SKILL_AVATAR_COLORS.length]
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

type ProjectPreviewKind = 'image' | 'markdown' | 'python' | 'pdf' | 'docx' | 'xlsx' | 'unsupported'

const PROJECT_IMAGE_SUFFIXES = new Set([
  '.bmp', '.gif', '.jpeg', '.jpg', '.png', '.svg', '.tif', '.tiff', '.webp',
])

function projectPreviewKind(fileName: string): ProjectPreviewKind {
  const dotIndex = fileName.lastIndexOf('.')
  const suffix = dotIndex >= 0 ? fileName.slice(dotIndex).toLowerCase() : ''
  if (PROJECT_IMAGE_SUFFIXES.has(suffix)) return 'image'
  if (suffix === '.md' || suffix === '.markdown') return 'markdown'
  if (suffix === '.py') return 'python'
  if (suffix === '.pdf') return 'pdf'
  if (suffix === '.docx') return 'docx'
  if (suffix === '.xlsx') return 'xlsx'
  return 'unsupported'
}

function formatProjectFileSize(sizeBytes?: number): string {
  if (typeof sizeBytes !== 'number') return ''
  if (sizeBytes < 1024) return `${sizeBytes} B`
  if (sizeBytes < 1024 * 1024) return `${(sizeBytes / 1024).toFixed(1)} KB`
  return `${(sizeBytes / (1024 * 1024)).toFixed(1)} MB`
}

function projectPreviewLabel(kind: ProjectPreviewKind): string {
  if (kind === 'image') return '图片'
  if (kind === 'markdown') return 'Markdown'
  if (kind === 'python') return 'Python'
  if (kind === 'pdf') return 'PDF'
  if (kind === 'docx') return 'Word'
  if (kind === 'xlsx') return 'Excel'
  return '文件'
}

/** 将 Markdown 中的相对资源路径限制在当前项目预览入口内。 */
function resolveProjectMarkdownUrl(projectId: string, filePath: string, url: string): string {
  if (/^(?:[a-z][a-z0-9+.-]*:|#|\/\/)/i.test(url)) return url
  const resourcePath = url.split(/[?#]/, 1)[0]
  const parts = resourcePath.startsWith('/')
    ? []
    : filePath.replace(/\\/g, '/').split('/').slice(0, -1)
  for (const rawPart of resourcePath.replace(/^\/+/, '').split('/')) {
    let part = rawPart
    try {
      part = decodeURIComponent(rawPart)
    } catch {
      // 非法转义保持原值，由后端统一判定路径是否合法。
    }
    if (!part || part === '.') continue
    if (part === '..') {
      if (!parts.length) return '#'
      parts.pop()
      continue
    }
    parts.push(part)
  }
  return parts.length ? projectFilePreviewUrl(projectId, parts.join('/')) : '#'
}

function ProjectPreviewSkeleton() {
  return (
    <div className="proj-preview-skeleton" aria-label="正在加载文件预览">
      <span />
      <span />
      <span />
      <span />
    </div>
  )
}

function ProjectFilePreviewPane({
  projectId,
  file,
}: {
  projectId: string
  file: ProjectFileNode
}) {
  const kind = projectPreviewKind(file.name)
  const previewUrl = projectFilePreviewUrl(projectId, file.path)
  const [preview, setPreview] = useState<ProjectFilePreview | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [activeSheet, setActiveSheet] = useState(0)

  useEffect(() => {
    setPreview(null)
    setError('')
    setActiveSheet(0)
    if (kind === 'image' || kind === 'pdf' || kind === 'unsupported') {
      setLoading(false)
      return
    }
    const controller = new AbortController()
    setLoading(true)
    void fetchProjectFilePreview(projectId, file.path, controller.signal)
      .then((data) => setPreview(data))
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return
        setError(reason instanceof Error ? reason.message : '无法读取文件预览')
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [file.path, kind, projectId])

  if (kind === 'unsupported') {
    return (
      <div className="proj-preview-state">
        <strong>此格式暂不支持侧栏预览</strong>
        <p>当前支持图片、Markdown、Excel（.xlsx）、PDF、Word（.docx）和 Python 文件。</p>
      </div>
    )
  }
  if (loading) return <ProjectPreviewSkeleton />
  if (error) {
    return (
      <div className="proj-preview-state error" role="alert">
        <strong>文件预览失败</strong>
        <p>{error}</p>
      </div>
    )
  }
  if (kind === 'image') {
    return (
      <div className="proj-preview-media is-image">
        <img
          src={previewUrl}
          alt={file.name}
          onError={() => setError('图片格式不受浏览器支持，或文件内容已经损坏')}
        />
      </div>
    )
  }
  if (kind === 'pdf') {
    return (
      <div className="proj-preview-media is-pdf">
        <iframe src={previewUrl} title={`${file.name} PDF 预览`} />
      </div>
    )
  }
  if (!preview) return null

  if (kind === 'markdown' && preview.kind === 'text') {
    return (
      <div className="proj-preview-markdown">
        <MarkdownContent
          content={preview.content}
          transformUrl={(url) => resolveProjectMarkdownUrl(projectId, file.path, url)}
        />
        {preview.truncated && <p className="proj-preview-note">文件较大，仅显示前部内容</p>}
      </div>
    )
  }
  if (kind === 'python' && preview.kind === 'text') {
    return (
      <div className="proj-preview-code">
        <pre><code>{preview.content}</code></pre>
        {preview.truncated && <p className="proj-preview-note">文件较大，仅显示前部内容</p>}
      </div>
    )
  }
  if (kind === 'docx' && preview.kind === 'docx') {
    const empty = !preview.paragraphs.length && !preview.tables.length
    return (
      <div className="proj-preview-document">
        {empty && <p className="proj-preview-empty">文档中没有可提取的文字或表格。</p>}
        {preview.paragraphs.map((paragraph, index) => (
          <p key={`paragraph-${index}`}>{paragraph}</p>
        ))}
        {preview.tables.map((rows, tableIndex) => (
          <div className="proj-preview-table-wrap" key={`table-${tableIndex}`}>
            <table>
              <tbody>
                {rows.map((row, rowIndex) => (
                  <tr key={`row-${rowIndex}`}>
                    {row.map((cell, cellIndex) => (
                      <td key={`cell-${cellIndex}`}>{cell}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
        {preview.truncated && <p className="proj-preview-note">文档较大，仅显示部分内容</p>}
      </div>
    )
  }
  if (kind === 'xlsx' && preview.kind === 'xlsx') {
    const sheet = preview.sheets[activeSheet]
    return (
      <div className="proj-preview-workbook">
        {preview.sheets.length > 1 && (
          <div className="proj-preview-tabs" role="tablist" aria-label="工作表">
            {preview.sheets.map((item, index) => (
              <button
                type="button"
                role="tab"
                aria-selected={activeSheet === index}
                className={activeSheet === index ? 'active' : ''}
                onClick={() => setActiveSheet(index)}
                key={item.name}
              >
                {item.name}
              </button>
            ))}
          </div>
        )}
        {sheet?.rows.length ? (
          <div className="proj-preview-table-wrap">
            <table>
              <tbody>
                {sheet.rows.map((row, rowIndex) => (
                  <tr key={`row-${rowIndex}`}>
                    {row.map((cell, cellIndex) => (
                      <td key={`cell-${cellIndex}`}>{cell == null ? '' : String(cell)}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <p className="proj-preview-empty">当前工作表为空。</p>}
        {preview.truncated && <p className="proj-preview-note">工作簿较大，仅显示部分内容</p>}
      </div>
    )
  }

  return (
    <div className="proj-preview-state error" role="alert">
      <strong>预览响应与文件类型不匹配</strong>
      <p>请刷新文件列表后重试。</p>
    </div>
  )
}

const ProjectFileTree = memo(function ProjectFileTree({
  nodes,
  depth = 0,
  expanded,
  onToggle,
  onSelect,
}: {
  nodes: ProjectFileNode[]
  depth?: number
  expanded: Set<string>
  onToggle: (path: string) => void
  onSelect: (file: ProjectFileNode) => void
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
                  onSelect={onSelect}
                />
              ) : null}
            </li>
          )
        }
        return (
          <li key={`file-${node.path}`}>
            <button
              type="button"
              className="proj-file-row file"
              onClick={() => onSelect(node)}
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
  const [selectedFile, setSelectedFile] = useState<ProjectFileNode | null>(null)
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
        setSelectedFile(null)
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

  useEffect(() => {
    if (!selectedFile) return
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') setSelectedFile(null)
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [selectedFile])

  if (selectedFile && projectId) {
    const kind = projectPreviewKind(selectedFile.name)
    const size = formatProjectFileSize(selectedFile.size_bytes)
    return (
      <>
        <div className="rag-sources-head proj-preview-head">
          <button
            type="button"
            className="proj-preview-back"
            onClick={() => setSelectedFile(null)}
            title="返回项目文件"
            aria-label="返回项目文件"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
              <path d="M15 18l-6-6 6-6" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
          <div className="proj-preview-title">
            <h2 title={selectedFile.name}>{selectedFile.name}</h2>
            <p title={selectedFile.path}>
              {projectPreviewLabel(kind)}{size ? ` · ${size}` : ''}
            </p>
          </div>
          <button
            type="button"
            className="rag-sources-collapse"
            onClick={onCollapse}
            title="收起项目文件"
            aria-label="收起项目文件"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
              <path d="M9 18l6-6-6-6" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
        </div>
        <div className={`proj-preview-body kind-${kind}`}>
          <ProjectFilePreviewPane projectId={projectId} file={selectedFile} />
        </div>
      </>
    )
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
            onSelect={setSelectedFile}
          />
        )}
      </div>
    </>
  )
}


/**
 * 输入框上方的任务卡片：按执行计划逐条列出任务，可展开或收起。
 * 收起后保留标题栏，用户随时能重新展开查看当前进度。
 */
const AgentTaskCard = memo(function AgentTaskCard({
  plan,
  todoState,
  working,
}: {
  plan: AgentPlan
  todoState: TodoStateMap
  /** Agent 是否仍在推进计划，用于标题栏的“进行中”文案与状态点动画 */
  working: boolean
}) {
  const [collapsed, setCollapsed] = useState(true)
  // 每次重新规划（新任务/新计划）时恢复默认收起状态，避免任务列表挤占对话空间
  const planKeyRef = useRef('')
  const planKey = `${plan.objective}|${plan.todos.map((todo) => todo.id).join('|')}`
  useEffect(() => {
    if (planKeyRef.current === planKey) return
    planKeyRef.current = planKey
    setCollapsed(true)
  }, [planKey])

  const items = plan.todos.map((todo) => ({
    id: todo.id,
    objective: todo.objective,
    status: todoState[todo.id] ?? ('pending' as TodoStatus),
  }))
  const total = items.length
  const completedCount = items.filter((item) => item.status === 'completed').length
  const failedCount = items.filter((item) => item.status === 'failed').length
  const terminatedCount = items.filter((item) => item.status === 'terminated').length
  const runningCount = items.filter((item) => item.status === 'in_progress').length
  const pendingCount = items.filter((item) => item.status === 'pending').length
  const progress = total ? Math.round((completedCount / total) * 100) : 0
  const panelId = 'agent-task-card-list'

  /**
   * 标题栏摘要：运行中优先看进行中的任务，结束后看整体完成度。
   * “进行中”只在摘要或后置高亮片段里出现一次，避免重复。
   */
  const summary = working && runningCount > 0
    ? `任务 ${runningCount} 进行中 · ${pendingCount} 待处理`
    : `任务 ${completedCount}/${total} 已完成`
  const summaryDetail = working && runningCount > 0
    ? ''
    : failedCount > 0
      ? `· ${failedCount} 失败`
      : terminatedCount > 0
        ? `· ${terminatedCount} 已终止`
        : runningCount > 0
          ? `· ${runningCount} 进行中`
          : ''

  return (
    <section
      className={`agent-task-card${collapsed ? ' is-collapsed' : ''}`}
      aria-label="任务列表"
    >
      <header className="agent-task-head">
        <button
          type="button"
          className="agent-task-toggle"
          aria-expanded={!collapsed}
          aria-controls={panelId}
          onClick={() => setCollapsed((value) => !value)}
          title={collapsed ? '展开任务列表' : '收起任务列表'}
        >
          <span className="agent-task-icon" aria-hidden="true">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9">
              <path d="M9.5 6.5h10M9.5 12h10M9.5 17.5h10" strokeLinecap="round" />
              <path d="M4 6.5l1.4 1.4L7.8 5.4M4 12l1.4 1.4L7.8 10.9M4 17.5l1.4 1.4L7.8 16.4" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </span>
          <span className="agent-task-title">
            <span className="agent-task-kicker">任务</span>
            <span className="agent-task-summary" aria-live="polite">
              {summary}
              {summaryDetail && <em>{summaryDetail}</em>}
            </span>
          </span>
        </button>
        <button
          type="button"
          className="agent-task-chevron"
          aria-expanded={!collapsed}
          aria-controls={panelId}
          onClick={() => setCollapsed((value) => !value)}
          title={collapsed ? '展开任务列表' : '收起任务列表'}
          aria-label={collapsed ? '展开任务列表' : '收起任务列表'}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
            <path d="M6 9.5l6 6 6-6" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      </header>
      {/* 进度条始终显示，收起后仍能看出整体推进程度 */}
      <div
        className="agent-task-progress"
        role="progressbar"
        aria-label="任务完成进度"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progress}
      >
        <span style={{ width: `${progress}%` }} />
      </div>
      {!collapsed && (
        <ol className="agent-task-list" id={panelId}>
          {items.map((item) => (
            <li
              className={`agent-task-item is-${item.status}`}
              key={item.id}
              aria-current={item.status === 'in_progress' ? 'step' : undefined}
            >
              {/* 任务不使用序号标记，仅用状态圆点区分进行中/已完成/失败 */}
              <span className="agent-task-dot" aria-hidden="true" />
              <p className="agent-task-objective" title={item.objective}>
                {summarizeTodoObjective(item.objective)}
              </p>
              <span className="agent-task-status">{TODO_STATUS_LABELS[item.status]}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
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
  plan,
  todoState,
}: {
  message: Message
  running?: boolean
  /** 当前讨论的执行计划与任务状态（由父组件统一推导，避免每条消息重复计算） */
  plan?: AgentPlan
  todoState?: TodoStateMap
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
              <AgentTrace
                trace={message.trace}
                running={running}
                plan={plan}
                todoState={todoState}
              />
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
  /** 跳转到「技能」管理页（「+」菜单底部“管理技能”） */
  onManageSkills?: () => void
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
  onManageSkills,
}: Props) {
  const [mode, setMode] = useState<Mode>(resumeRunId ? 'modeling' : 'agent')
  const [draft, setDraft] = useState('')
  const [messages, setMessages] = useState<Message[]>([])
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [activeRunId, setActiveRunId] = useState<string | null>(null)
  const [isStopping, setIsStopping] = useState(false)
  const [files, setFiles] = useState<File[]>([])
  const [skills, setSkills] = useState<SkillInfo[]>([])
  const [selectedSkillIds, setSelectedSkillIds] = useState<string[]>([])
  // 「+」菜单：技能搜索关键词、导入中标记与导入失败提示
  const [skillQuery, setSkillQuery] = useState('')
  const [skillImporting, setSkillImporting] = useState(false)
  const [skillImportError, setSkillImportError] = useState('')
  const [title, setTitle] = useState('新讨论')
  const [agentTrace, setAgentTrace] = useState<AgentTraceEvent[]>([])
  const [agentRunTerminated, setAgentRunTerminated] = useState(false)
  // 当前讨论的执行计划与任务状态：驱动输入框上方的任务卡片
  const agentPlan = useMemo(() => latestAgentPlan(agentTrace), [agentTrace])
  const agentTodoState = useMemo(
    () => deriveTodoStates(agentPlan, agentTrace, agentRunTerminated),
    [agentPlan, agentTrace, agentRunTerminated],
  )
  const agentPlanCounts = useMemo(
    () => countTodoStates(agentPlan, agentTodoState),
    [agentPlan, agentTodoState],
  )
  // 右侧项目文件面板默认收起，由顶部按钮开关
  const [sourcesCollapsed, setSourcesCollapsed] = useState(true)
  const [projectSidebarWidth, setProjectSidebarWidth] = useState(loadProjectSidebarWidth)
  const [apiConfigs, setApiConfigs] = useState<ApiConfig[]>([])
  const [selectedConfigId, setSelectedConfigId] = useState<string>('')
  const apiConfigsRef = useRef<ApiConfig[]>([])
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseSummary[]>([])
  // 不预选知识库，用户可以选择零个或多个
  const [knowledgeBaseIds, setKnowledgeBaseIds] = useState<string[]>([])
  const [expandedKnowledgeBaseId, setExpandedKnowledgeBaseId] = useState<string | null>(null)
  const knowledgeBasesRef = useRef<KnowledgeBaseSummary[]>([])
  const fileInputRef = useRef<HTMLInputElement>(null)
  // 「+」菜单：主弹层、技能飞层与「从本地添加技能」文件输入
  const addPickerRef = useRef<HTMLDetailsElement>(null)
  const skillSubRef = useRef<HTMLDetailsElement>(null)
  const skillImportInputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const chatBodyRef = useRef<HTMLDivElement>(null)
  const projectSidebarWidthRef = useRef(projectSidebarWidth)
  const projectSidebarResizeRef = useRef<{
    pointerId: number
    startX: number
    startWidth: number
  } | null>(null)
  // 人工批准弹窗：请求内容 + 结算回调（Promise 流内等待用户点按钮）
  const [approval, setApproval] = useState<ApprovalRequest | null>(null)
  const approvalResolveRef = useRef<((approved: boolean) => void) | null>(null)
  const streamControllerRef = useRef<AbortController | null>(null)
  const runningDiscussionRef = useRef<string | null>(null)
  const detachedRef = useRef(false)

  useEffect(() => {
    detachedRef.current = false
    return () => {
      detachedRef.current = true
      // 尚未收到任务编号时保持提交请求，待后端启动后再断开订阅。
      if (runningDiscussionRef.current) streamControllerRef.current?.abort()
      if (approvalResolveRef.current) approvalResolveRef.current(false)
    }
  }, [])

  function setProjectSidebarCssWidth(width: number): number {
    const containerWidth = chatBodyRef.current?.getBoundingClientRect().width
    const nextWidth = clampProjectSidebarWidth(width, containerWidth)
    projectSidebarWidthRef.current = nextWidth
    chatBodyRef.current?.style.setProperty('--project-sidebar-width', `${nextWidth}px`)
    return nextWidth
  }

  function saveProjectSidebarWidth(width: number) {
    const nextWidth = setProjectSidebarCssWidth(width)
    setProjectSidebarWidth(nextWidth)
    try {
      window.localStorage.setItem(PROJECT_SIDEBAR_WIDTH_STORAGE_KEY, String(nextWidth))
    } catch {
      // 浏览器禁用本地存储时仅保留当前页面宽度。
    }
  }

  function handleProjectSidebarResizeStart(event: React.PointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return
    event.preventDefault()
    projectSidebarResizeRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startWidth: projectSidebarWidthRef.current,
    }
    event.currentTarget.setPointerCapture(event.pointerId)
    document.body.classList.add('is-resizing-project-sidebar')
  }

  function handleProjectSidebarResizeMove(event: React.PointerEvent<HTMLDivElement>) {
    const resize = projectSidebarResizeRef.current
    if (!resize || resize.pointerId !== event.pointerId) return
    setProjectSidebarCssWidth(resize.startWidth + resize.startX - event.clientX)
  }

  function finishProjectSidebarResize(
    event: React.PointerEvent<HTMLDivElement>,
  ) {
    const resize = projectSidebarResizeRef.current
    if (!resize || resize.pointerId !== event.pointerId) return
    projectSidebarResizeRef.current = null
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
    document.body.classList.remove('is-resizing-project-sidebar')
    saveProjectSidebarWidth(projectSidebarWidthRef.current)
  }

  function handleProjectSidebarResizeKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    const step = event.shiftKey ? 48 : 16
    let nextWidth: number | null = null
    if (event.key === 'ArrowLeft') nextWidth = projectSidebarWidthRef.current + step
    if (event.key === 'ArrowRight') nextWidth = projectSidebarWidthRef.current - step
    if (event.key === 'Home') nextWidth = PROJECT_SIDEBAR_MIN_WIDTH
    if (event.key === 'End') nextWidth = PROJECT_SIDEBAR_MAX_WIDTH
    if (nextWidth === null) return
    event.preventDefault()
    saveProjectSidebarWidth(nextWidth)
  }

  useEffect(() => {
    function handleWindowResize() {
      const currentWidth = projectSidebarWidthRef.current
      const nextWidth = setProjectSidebarCssWidth(projectSidebarWidthRef.current)
      if (nextWidth !== currentWidth) {
        setProjectSidebarWidth(nextWidth)
      }
    }
    handleWindowResize()
    window.addEventListener('resize', handleWindowResize)
    return () => {
      window.removeEventListener('resize', handleWindowResize)
      document.body.classList.remove('is-resizing-project-sidebar')
    }
  }, [])

  function createStreamHandlers(controller: AbortController): AgentChatHandlers {
    const active = () => !detachedRef.current && !controller.signal.aborted
    return {
      onStarted: (runId, _files, savedDiscussionId) => {
        if (detachedRef.current) {
          controller.abort()
          return
        }
        if (!active()) return
        setActiveRunId(runId)
        setAgentRunTerminated(false)
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
            shell: event.shell,
            timeout_seconds: event.timeout_seconds,
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
        setAgentRunTerminated(false)
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
        setAgentRunTerminated(true)
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
    apiConfigsRef.current = configs
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
    void listSkills()
      .then((items) => {
        setSkills(items)
        setSelectedSkillIds((current) => {
          const available = new Set(items.map((item) => item.skill_id))
          return current.filter((id) => available.has(id))
        })
      })
      .catch(() => setSkills([]))
  }, [])

  // 技能飞层搜索过滤：名称 / ID / 描述任一命中即可
  const filteredSkills = useMemo(() => {
    const keyword = skillQuery.trim().toLowerCase()
    if (!keyword) return skills
    return skills.filter((skill) => (
      skill.name.toLowerCase().includes(keyword)
      || skill.skill_id.toLowerCase().includes(keyword)
      || skill.description.toLowerCase().includes(keyword)
    ))
  }, [skills, skillQuery])

  // 「+」菜单：点击外部或按 Esc 收起，避免弹层滞留遮挡输入区
  useEffect(() => {
    function handlePointerDown(event: PointerEvent) {
      const picker = addPickerRef.current
      if (!picker?.open) return
      const target = event.target
      if (target instanceof Node && picker.contains(target)) return
      picker.open = false
    }
    function handleKeyDown(event: KeyboardEvent) {
      const picker = addPickerRef.current
      if (picker?.open && event.key === 'Escape') picker.open = false
    }
    document.addEventListener('pointerdown', handlePointerDown)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('pointerdown', handlePointerDown)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [])

  /** 从本地 ZIP 包导入技能：成功后刷新列表并默认勾选新技能 */
  async function importSkillArchive(file: File) {
    setSkillImporting(true)
    setSkillImportError('')
    try {
      const imported = await importSkill(file)
      setSkills(await listSkills())
      setSelectedSkillIds((current) => (
        current.includes(imported.skill_id) ? current : [...current, imported.skill_id]
      ))
    } catch (reason) {
      setSkillImportError(reason instanceof Error ? reason.message : '导入技能失败')
    } finally {
      setSkillImporting(false)
      if (skillImportInputRef.current) skillImportInputRef.current.value = ''
    }
  }

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
    setAgentRunTerminated(false)
    setSelectedSkillIds([])
    if (!discussionId) {
      setMessages([])
      setTitle('新讨论')
      setAgentTrace([])
      setSelectedConfigId(apiConfigsRef.current[0]?.id ?? '')
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
        const availableConfigs = apiConfigsRef.current
        setSelectedConfigId(
          discussion.model_config_id
          && (
            !availableConfigs.length
            || availableConfigs.some((config) => config.id === discussion.model_config_id)
          )
            ? discussion.model_config_id
            : (availableConfigs[0]?.id ?? ''),
        )
        if (activeRun) {
          const controller = new AbortController()
          streamControllerRef.current = controller
          runningDiscussionRef.current = discussionId
          setActiveRunId(activeRun.run_id)
          setIsSubmitting(true)
          setAgentRunTerminated(false)
          void reconnectAgentChatRun(activeRun.run_id, createStreamHandlers(controller), controller.signal)
            .catch((error) => {
              if (controller.signal.aborted) return
              setAgentRunTerminated(true)
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
    setAgentRunTerminated(false)
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
        selectedSkillIds,
        controller.signal,
      )
    } catch (error) {
      if (controller.signal.aborted || detachedRef.current) return
      setAgentRunTerminated(true)
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
      if (!controller.signal.aborted && !detachedRef.current) {
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

  async function selectModel(configId: string) {
    const previousConfigId = selectedConfigId
    setSelectedConfigId(configId)
    if (!discussionId) return
    try {
      await updateBrainstormDiscussionModel(discussionId, configId)
    } catch {
      setSelectedConfigId((current) => current === configId ? previousConfigId : current)
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

      <div
        className={`rag-chat-body ${sourcesCollapsed ? 'sources-collapsed' : ''}`}
        ref={chatBodyRef}
        style={{ '--project-sidebar-width': `${projectSidebarWidth}px` } as React.CSSProperties}
      >
        <section className="rag-chat-col" aria-label="聊天消息">
          <div className="rag-messages" ref={listRef}>
            <div className="rag-messages-inner">
              {messages.map((message, index) => (
                <ChatMessage
                  key={`${message.role}-${index}`}
                  message={message}
                  running={isSubmitting && index === messages.length - 1}
                  plan={agentPlan}
                  todoState={agentTodoState}
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
            {agentPlan && (
              // 任务卡片固定在输入框上方，可展开/收起查看完整任务列表
              <AgentTaskCard
                plan={agentPlan}
                todoState={agentTodoState}
                working={isSubmitting && (agentPlanCounts.inProgress > 0 || agentPlanCounts.pending > 0)}
              />
            )}
            {approval ? (
              <CommandApprovalDialog
                command={approval.command}
                workspaceRoot={approval.workspace_root}
                shell={approval.shell}
                timeoutSeconds={approval.timeout_seconds}
                onApprove={() => settleApproval(true)}
                onReject={() => settleApproval(false)}
              />
            ) : null}
            <div className="rag-composer">
              {(files.length > 0 || selectedSkillIds.length > 0) && (
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
                  {selectedSkillIds.map((skillId) => (
                    <span className="rag-file-chip rag-skill-chip" key={skillId}>
                      技能 · {skills.find((item) => item.skill_id === skillId)?.name ?? skillId}
                      <button
                        type="button"
                        aria-label="移除技能"
                        onClick={() => setSelectedSkillIds((current) => current.filter((id) => id !== skillId))}
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
                    {knowledgeBases.map((base) => {
                      const descriptionId = `rag-kb-description-${base.id}`
                      const descriptionExpanded = expandedKnowledgeBaseId === base.id
                      return (
                        <div className="rag-kb-option" key={base.id}>
                          <label>
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
                          <button
                            type="button"
                            className="rag-kb-description-toggle"
                            aria-label={`查看${base.name}的知识库描述`}
                            aria-expanded={descriptionExpanded}
                            aria-controls={descriptionId}
                            onClick={() => setExpandedKnowledgeBaseId((current) => (
                              current === base.id ? null : base.id
                            ))}
                          >
                            ···
                          </button>
                          {descriptionExpanded && (
                            <p id={descriptionId} className="rag-kb-description">
                              {base.description.trim() || '暂无描述'}
                            </p>
                          )}
                        </div>
                      )
                    })}
                  </div>
                </details>
                <details
                  className="rag-add-picker"
                  ref={addPickerRef}
                  onToggle={(event) => {
                    // 收起主菜单时同步复位技能飞层，避免下次打开残留展开状态
                    if (event.currentTarget.open) return
                    if (skillSubRef.current) skillSubRef.current.open = false
                    setSkillQuery('')
                    setSkillImportError('')
                  }}
                >
                  <summary
                    className={isSubmitting ? 'is-disabled' : undefined}
                    aria-label="添加文件或加载技能"
                    title="添加文件或加载技能"
                  >
                    +
                  </summary>
                  <div className="rag-add-menu">
                    <button
                      type="button"
                      className="rag-add-item"
                      disabled={isSubmitting}
                      onClick={(event) => {
                        fileInputRef.current?.click()
                        const details = event.currentTarget.closest('details.rag-add-picker')
                        if (details instanceof HTMLDetailsElement) details.open = false
                      }}
                    >
                      {/* 曲别针：选择本地文件作为附件 */}
                      <svg className="rag-add-item-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                        <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
                      </svg>
                      <span className="rag-add-item-label">添加文件</span>
                      {files.length > 0 && <span className="rag-add-item-count">{files.length}</span>}
                    </button>
                    <details className="rag-add-sub" ref={skillSubRef}>
                      <summary className="rag-add-item" aria-label="加载技能" title="加载技能">
                        {/* 魔法棒：加载已有技能 */}
                        <svg className="rag-add-item-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                          <path d="M5 19L14.5 9.5" />
                          <path d="M18.6 3.4l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8.8-2.2z" />
                          <path d="M6.5 3.8l.6 1.6 1.6.6-1.6.6-.6 1.6-.6-1.6-1.6-.6 1.6-.6.6-1.6z" />
                        </svg>
                        <span className="rag-add-item-label">技能</span>
                        {selectedSkillIds.length > 0 && (
                          <span className="rag-add-item-count">{selectedSkillIds.length}</span>
                        )}
                        <svg className="rag-add-item-chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                          <path d="M9 6l6 6-6 6" />
                        </svg>
                      </summary>
                      <div className="rag-skill-flyout">
                        <div className="rag-skill-search">
                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
                            <circle cx="11" cy="11" r="6" />
                            <path d="M16 16l4 4" />
                          </svg>
                          <input
                            type="search"
                            value={skillQuery}
                            onChange={(event) => setSkillQuery(event.target.value)}
                            placeholder="搜索技能"
                            aria-label="搜索技能"
                          />
                        </div>
                        <div className="rag-skill-list">
                          {skills.length === 0 ? (
                            <p className="rag-skill-empty">暂无已安装技能</p>
                          ) : filteredSkills.length === 0 ? (
                            <p className="rag-skill-empty">没有匹配「{skillQuery.trim()}」的技能</p>
                          ) : filteredSkills.map((skill) => {
                            const selected = selectedSkillIds.includes(skill.skill_id)
                            return (
                              <button
                                type="button"
                                key={skill.skill_id}
                                className={`rag-skill-item${selected ? ' is-selected' : ''}`}
                                aria-pressed={selected}
                                disabled={isSubmitting}
                                title={skill.description || skill.name}
                                onClick={() => setSelectedSkillIds((current) => (
                                  current.includes(skill.skill_id)
                                    ? current.filter((id) => id !== skill.skill_id)
                                    : [...current, skill.skill_id]
                                ))}
                              >
                                <span
                                  className="rag-skill-avatar"
                                  style={{ background: skillAvatarColor(skill.skill_id) }}
                                  aria-hidden="true"
                                >
                                  {skillAvatarLabel(skill.name, skill.skill_id)}
                                </span>
                                <span className="rag-skill-meta">
                                  <span className="rag-skill-name">{skill.name}</span>
                                  <span className="rag-skill-desc">{skill.description || '未填写说明'}</span>
                                </span>
                                {selected && (
                                  <svg className="rag-skill-check" width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                                    <path d="M5 12.5l4.5 4.5L19 7" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
                                  </svg>
                                )}
                              </button>
                            )
                          })}
                        </div>
                        <div className="rag-skill-actions">
                          <button
                            type="button"
                            className="rag-add-item"
                            disabled={isSubmitting || skillImporting}
                            onClick={() => skillImportInputRef.current?.click()}
                          >
                            {/* 上箭头：从本地导入 ZIP 技能包 */}
                            <svg className="rag-add-item-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                              <path d="M17 8l-5-5-5 5" />
                              <path d="M12 3v12" />
                            </svg>
                            <span className="rag-add-item-label">
                              {skillImporting ? '正在导入…' : '从本地添加技能'}
                            </span>
                          </button>
                          {onManageSkills && (
                            <button
                              type="button"
                              className="rag-add-item"
                              onClick={(event) => {
                                const details = event.currentTarget.closest('details.rag-add-picker')
                                if (details instanceof HTMLDetailsElement) details.open = false
                                onManageSkills()
                              }}
                            >
                              {/* 齿轮：打开技能管理页 */}
                              <svg className="rag-add-item-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                                <circle cx="12" cy="12" r="3" />
                                <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
                              </svg>
                              <span className="rag-add-item-label">管理技能</span>
                            </button>
                          )}
                          {skillImportError && (
                            <p className="rag-skill-error" role="alert">{skillImportError}</p>
                          )}
                        </div>
                      </div>
                    </details>
                    <input
                      ref={skillImportInputRef}
                      hidden
                      type="file"
                      accept=".zip,application/zip"
                      onChange={(event) => {
                        const file = event.target.files?.[0]
                        if (file) void importSkillArchive(file)
                      }}
                    />
                  </div>
                </details>
                <input
                  ref={fileInputRef}
                  hidden
                  type="file"
                  multiple
                  accept=".csv,.xlsx,.xls,.json,.jsonl,.ndjson,.pdf,.docx,.md,.markdown,.py,.txt,.png,.jpg,.jpeg,.webp,.gif,.bmp,.tif,.tiff"
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
                                void selectModel(config.id)
                                const details = event.currentTarget.closest('details')
                                if (details instanceof HTMLDetailsElement) details.open = false
                              }}
                            >
                              <span className="rag-model-option-main">
                                <span className="rag-model-option-name">{label}</span>
                                {config.reasoningEffort && config.reasoningEffort !== 'close' ? (
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
                  <span className="rag-model-empty" title="尚未在设置中配置模型">
                    未配置模型
                  </span>
                )}
                <button
                  type="button"
                  className={`rag-send${isSubmitting ? ' rag-stop' : ''}`}
                  onClick={() => isSubmitting ? void stopActiveRun() : void submit()}
                  disabled={isSubmitting ? !activeRunId || isStopping : (!draft.trim() && files.length === 0)}
                  aria-label={isSubmitting ? (isStopping ? '正在终止任务' : '终止任务') : '发送'}
                  title={isSubmitting ? (isStopping ? '正在终止任务' : '终止当前任务') : '发送'}
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
          <div
            className="rag-sources-resizer"
            role="separator"
            aria-label="调整项目文件侧栏宽度"
            aria-orientation="vertical"
            aria-valuemin={PROJECT_SIDEBAR_MIN_WIDTH}
            aria-valuemax={PROJECT_SIDEBAR_MAX_WIDTH}
            aria-valuenow={projectSidebarWidth}
            aria-valuetext={`${projectSidebarWidth} 像素`}
            tabIndex={0}
            onPointerDown={handleProjectSidebarResizeStart}
            onPointerMove={handleProjectSidebarResizeMove}
            onPointerUp={finishProjectSidebarResize}
            onPointerCancel={finishProjectSidebarResize}
            onLostPointerCapture={finishProjectSidebarResize}
            onKeyDown={handleProjectSidebarResizeKeyDown}
            title="左右拖动调整宽度"
          />
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

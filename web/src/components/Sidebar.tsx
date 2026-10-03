import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

import {
  createProject,
  deleteBrainstormDiscussion,
  deleteProject,
  fetchBrainstormDiscussions,
  fetchProjects,
  renameBrainstormDiscussion,
  selectSystemFolder,
  type BrainstormDiscussionSummary,
  type ProjectSummary,
} from '../api'
import ConfirmDialog from './ConfirmDialog'

/** 侧边栏拖拽宽度：与项目侧栏同一套 pointer 拖拽 + localStorage 记忆 */
const SIDEBAR_DEFAULT_WIDTH = 260
const SIDEBAR_MIN_WIDTH = 200
const SIDEBAR_MAX_WIDTH = 420
const SIDEBAR_MIN_CONTENT_WIDTH = 480
const SIDEBAR_WIDTH_STORAGE_KEY = 'daagent.sidebar-width'

function clampSidebarWidth(width: number, viewportWidth?: number): number {
  const available = viewportWidth ?? (typeof window === 'undefined' ? SIDEBAR_DEFAULT_WIDTH : window.innerWidth)
  const responsiveMax = Math.max(
    SIDEBAR_MIN_WIDTH,
    Math.min(SIDEBAR_MAX_WIDTH, available - SIDEBAR_MIN_CONTENT_WIDTH),
  )
  return Math.round(Math.min(responsiveMax, Math.max(SIDEBAR_MIN_WIDTH, width)))
}

function loadSidebarWidth(): number {
  if (typeof window === 'undefined') return SIDEBAR_DEFAULT_WIDTH
  try {
    const saved = Number(window.localStorage.getItem(SIDEBAR_WIDTH_STORAGE_KEY))
    if (Number.isFinite(saved) && saved > 0) return clampSidebarWidth(saved)
  } catch {
    // 浏览器禁用本地存储时使用默认宽度
  }
  return SIDEBAR_DEFAULT_WIDTH
}

type Props = {
  section: string
  collapsed: boolean
  activeDiscussionId: string | null
  activeProjectId: string | null
  onProjectChange: (projectId: string | null) => void
  onHome: () => void
  onModeling: () => void
  onBrainstorm: () => void
  onConverter: () => void
  onTaskExpert: () => void
  onSkills: () => void
  onOpenDiscussion: (id: string, projectId: string | null) => void
  onSettings: () => void
  onDiscussionDeleted?: (id: string) => void
}

const LOGO = (
  <img className="sidebar-logo-img" src="/logo.png" alt="" aria-hidden="true" />
)

export default function Sidebar({
  section,
  collapsed,
  activeDiscussionId,
  activeProjectId,
  onProjectChange,
  onHome,
  onModeling,
  onBrainstorm,
  onConverter,
  onTaskExpert,
  onSkills,
  onOpenDiscussion,
  onSettings,
  onDiscussionDeleted,
}: Props) {
  const [discussions, setDiscussions] = useState<BrainstormDiscussionSummary[]>([])
  const [projects, setProjects] = useState<ProjectSummary[]>([])
  const [projectError, setProjectError] = useState('')
  const [menuState, setMenuState] = useState<{
    id: string
    top: number
    left: number
  } | null>(null)
  const [pendingRemoveProject, setPendingRemoveProject] = useState<ProjectSummary | null>(null)
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renameDraft, setRenameDraft] = useState('')
  const [isResizing, setIsResizing] = useState(false)
  const sidebarWidthRef = useRef(SIDEBAR_DEFAULT_WIDTH)
  const resizeStateRef = useRef<{ pointerId: number; startX: number; startWidth: number } | null>(null)
  const [pendingDelete, setPendingDelete] = useState<BrainstormDiscussionSummary | null>(null)
  const [busy, setBusy] = useState(false)
  const [projectsExpanded, setProjectsExpanded] = useState(true)
  const [expandedProjectIds, setExpandedProjectIds] = useState<Set<string>>(new Set())

  useEffect(() => {
    void fetchProjects()
      .then((items) => {
        setProjects(items)
        if (!activeProjectId && items.length) {
          setExpandedProjectIds(new Set([items[0].id]))
          onProjectChange(items[0].id)
        }
      })
      .catch(() => setProjects([]))
  }, [])

  useEffect(() => {
    void fetchBrainstormDiscussions()
      .then(setDiscussions)
      .catch(() => setDiscussions([]))
  }, [section, activeDiscussionId])

  useEffect(() => {
    if (!activeProjectId) return
    setExpandedProjectIds((current) => {
      if (current.has(activeProjectId)) return current
      const next = new Set(current)
      next.add(activeProjectId)
      return next
    })
  }, [activeProjectId])

  useEffect(() => {
    if (section !== 'modeling') return
    const timer = window.setInterval(() => {
      void fetchBrainstormDiscussions().then(setDiscussions).catch(() => {})
    }, 5000)
    return () => window.clearInterval(timer)
  }, [section])

  useEffect(() => {
    if (!menuState) return
    const onDocClick = (e: MouseEvent) => {
      const target = e.target as HTMLElement | null
      if (target?.closest('.session-menu') || target?.closest('.session-more')) return
      setMenuState(null)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMenuState(null)
    }
    // 等本次点击结束后再挂监听，避免“打开即被关掉”
    const timer = window.setTimeout(() => {
      document.addEventListener('click', onDocClick)
      document.addEventListener('keydown', onKey)
    }, 0)
    return () => {
      window.clearTimeout(timer)
      document.removeEventListener('click', onDocClick)
      document.removeEventListener('keydown', onKey)
    }
  }, [menuState])

  async function handleAddProject() {
    setProjectError('')
    let rootPath: string | null
    try {
      rootPath = await selectSystemFolder()
    } catch (error) {
      setProjectError(error instanceof Error ? error.message : '无法打开系统文件夹选择器')
      return
    }
    if (!rootPath) return
    try {
      const project = await createProject(rootPath)
      setProjects(await fetchProjects())
      setExpandedProjectIds((current) => new Set(current).add(project.id))
      onProjectChange(project.id)
    } catch (error) {
      setProjectError(error instanceof Error ? error.message : '添加项目失败')
    }
  }

  async function handleRemoveProject() {
    const project = projects.find((item) => item.id === activeProjectId)
    if (!project) return
    setPendingRemoveProject(project)
  }

  async function confirmRemoveProject() {
    const project = pendingRemoveProject
    if (!project) return
    setPendingRemoveProject(null)
    setProjectError('')
    try {
      await deleteProject(project.id)
      const remaining = await fetchProjects()
      setProjects(remaining)
      setDiscussions(await fetchBrainstormDiscussions())
      setExpandedProjectIds((current) => {
        const next = new Set(current)
        next.delete(project.id)
        return next
      })
      onProjectChange(remaining[0]?.id ?? null)
    } catch (error) {
      setProjectError(error instanceof Error ? error.message : '移除项目失败')
    }
  }

  async function refresh() {
    try {
      setDiscussions(await fetchBrainstormDiscussions())
    } catch {
      /* keep current list on failure */
    }
  }

  function toggleProject(project: ProjectSummary) {
    setExpandedProjectIds((current) => {
      const next = new Set(current)
      if (next.has(project.id)) next.delete(project.id)
      else next.add(project.id)
      return next
    })
    if (project.id !== activeProjectId) onProjectChange(project.id)
  }

  async function handleRenameSubmit(item: BrainstormDiscussionSummary) {
    const title = renameDraft.trim()
    if (!title || busy) return
    setBusy(true)
    try {
      await renameBrainstormDiscussion(item.id, title)
      setRenamingId(null)
      setMenuState(null)
      await refresh()
    } catch {
      /* stay in edit mode so the user can retry */
    } finally {
      setBusy(false)
    }
  }

  function openSessionMenu(e: React.MouseEvent<HTMLButtonElement>, item: BrainstormDiscussionSummary) {
    e.preventDefault()
    e.stopPropagation()
    if (menuState?.id === item.id) {
      setMenuState(null)
      return
    }
    const rect = e.currentTarget.getBoundingClientRect()
    setMenuState({
      id: item.id,
      top: rect.bottom + 4,
      left: Math.min(rect.right - 120, window.innerWidth - 130),
    })
  }

  async function handleDeleteConfirm() {
    if (!pendingDelete || busy) return
    setBusy(true)
    try {
      await deleteBrainstormDiscussion(pendingDelete.id)
      const deletedId = pendingDelete.id
      setPendingDelete(null)
      setMenuState(null)
      if (activeDiscussionId === deletedId) {
        onDiscussionDeleted?.(deletedId)
      }
      await refresh()
    } catch {
      /* keep dialog open on failure */
    } finally {
      setBusy(false)
    }
  }

  /** 把侧边栏宽度写到 CSS 变量，主内容区 margin-left 会跟着变 */
  function applySidebarWidth(width: number): number {
    const next = clampSidebarWidth(width)
    sidebarWidthRef.current = next
    document.documentElement.style.setProperty('--sidebar-width', `${next}px`)
    return next
  }

  function saveSidebarWidth(width: number) {
    const next = applySidebarWidth(width)
    try {
      window.localStorage.setItem(SIDEBAR_WIDTH_STORAGE_KEY, String(next))
    } catch {
      // 浏览器禁用本地存储时仅保留当前页面宽度
    }
  }

  function handleResizeStart(event: React.PointerEvent<HTMLDivElement>) {
    if (event.button !== 0 || collapsed) return
    event.preventDefault()
    resizeStateRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startWidth: sidebarWidthRef.current,
    }
    event.currentTarget.setPointerCapture(event.pointerId)
    setIsResizing(true)
    document.body.classList.add('is-resizing-app-sidebar')
  }

  function handleResizeMove(event: React.PointerEvent<HTMLDivElement>) {
    const resize = resizeStateRef.current
    if (!resize || resize.pointerId !== event.pointerId) return
    applySidebarWidth(resize.startWidth + event.clientX - resize.startX)
  }

  function finishResize(event: React.PointerEvent<HTMLDivElement>) {
    const resize = resizeStateRef.current
    if (!resize || resize.pointerId !== event.pointerId) return
    resizeStateRef.current = null
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
    setIsResizing(false)
    document.body.classList.remove('is-resizing-app-sidebar')
    saveSidebarWidth(sidebarWidthRef.current)
  }

  function handleResizeKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    if (collapsed) return
    const step = event.shiftKey ? 48 : 16
    let next: number | null = null
    if (event.key === 'ArrowLeft') next = sidebarWidthRef.current - step
    if (event.key === 'ArrowRight') next = sidebarWidthRef.current + step
    if (event.key === 'Home') next = SIDEBAR_MIN_WIDTH
    if (event.key === 'End') next = SIDEBAR_MAX_WIDTH
    if (next === null) return
    event.preventDefault()
    saveSidebarWidth(next)
  }

  // 挂载时恢复上次宽度；窗口变化时重新夹取合法区间
  useEffect(() => {
    applySidebarWidth(loadSidebarWidth())
    function handleWindowResize() {
      applySidebarWidth(sidebarWidthRef.current)
    }
    window.addEventListener('resize', handleWindowResize)
    return () => {
      window.removeEventListener('resize', handleWindowResize)
      document.body.classList.remove('is-resizing-app-sidebar')
    }
  }, [])

  return (
    <aside
      className={`sidebar ${collapsed ? 'sidebar--collapsed' : ''} ${isResizing ? 'is-resizing' : ''}`}
      aria-hidden={collapsed}
    >
      <div
        className="sidebar-resizer"
        role="separator"
        aria-orientation="vertical"
        aria-label="调整侧边栏宽度"
        aria-valuemin={SIDEBAR_MIN_WIDTH}
        aria-valuemax={SIDEBAR_MAX_WIDTH}
        aria-valuenow={sidebarWidthRef.current}
        tabIndex={collapsed ? -1 : 0}
        onPointerDown={handleResizeStart}
        onPointerMove={handleResizeMove}
        onPointerUp={finishResize}
        onPointerCancel={finishResize}
        onKeyDown={handleResizeKeyDown}
      />
      <div className="sidebar-top">
        <div className="sidebar-logo" onClick={onHome} title="DAAgent 数据分析智能体">
          {LOGO}
          <div className="logo-text sidebar-collapse-hide">
            <span className="logo-name sidebar-brand"><span className="sidebar-brand-prefix">DA</span>Agent</span>
            <span className="logo-sub sidebar-sub">数据分析智能体</span>
          </div>
        </div>
      </div>

      <div className="rail-section">
        <button
          className={`nav-item ${section === 'modeling' ? 'active' : ''}`}
          onClick={onModeling}
          title={collapsed ? '新建任务' : undefined}
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
            <path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z" />
          </svg>
          <span className="nav-text sidebar-collapse-hide">新建任务</span>
        </button>
        <button
          className={`nav-item ${section === 'brainstorm' ? 'active' : ''}`}
          onClick={onBrainstorm}
          title={collapsed ? '知识库' : undefined}
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
            <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
            <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
          </svg>
          <span className="nav-text sidebar-collapse-hide">知识库</span>
        </button>
        <button
          className={`nav-item ${section === 'converter' ? 'active' : ''}`}
          onClick={onConverter}
          title={collapsed ? '文档转换' : undefined}
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M4 8h12" />
            <path d="m13 5 3 3-3 3" />
            <path d="M20 16H8" />
            <path d="m11 13-3 3 3 3" />
          </svg>
          <span className="nav-text sidebar-collapse-hide">文档转换</span>
        </button>
        <button
          className={`nav-item ${section === 'task-expert' ? 'active' : ''}`}
          onClick={onTaskExpert}
          title={collapsed ? '任务专家' : undefined}
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M12 3l2.2 4.5L19 8.2l-3.5 3.4.8 4.9L12 14.2 7.7 16.5l.8-4.9L5 8.2l4.8-.7L12 3z" />
            <path d="M5 20h14" />
          </svg>
          <span className="nav-text sidebar-collapse-hide">任务专家</span>
        </button>
        <button
          className={`nav-item ${section === 'skills' ? 'active' : ''}`}
          onClick={onSkills}
          title={collapsed ? '技能' : undefined}
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M14.7 6.3a4 4 0 1 0-5.4 5.4L4 17v3h3l5.3-5.3a4 4 0 0 0 2.4-5.4z" />
            <path d="M15 8h.01" />
          </svg>
          <span className="nav-text sidebar-collapse-hide">技能</span>
        </button>
      </div>

      {/* 项目入口：分组列表形态，底层仍走 onProjectChange / add / remove */}
      <div className="rail-section project-section">
        <div className="project-section-head sidebar-collapse-hide">
          <button
            type="button"
            className="project-section-toggle"
            onClick={() => setProjectsExpanded((open) => !open)}
            aria-expanded={projectsExpanded}
          >
            <svg
              className={`project-section-chevron ${projectsExpanded ? 'is-open' : ''}`}
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <path d="M6 9l6 6 6-6" />
            </svg>
            <span>项目</span>
          </button>
          <div className="project-section-actions">
            <button type="button" onClick={() => void handleAddProject()} title="添加项目" aria-label="添加项目">＋</button>
            <button type="button" onClick={() => void handleRemoveProject()} disabled={!activeProjectId} title="移除项目" aria-label="移除项目">−</button>
          </div>
        </div>
        {(projectsExpanded || collapsed) && (
          <div className="project-list" role="list">
            {projects.length === 0 ? (
              <p className="project-list-empty sidebar-collapse-hide">暂无项目</p>
            ) : (
              projects.map((project) => {
                const isActive = project.id === activeProjectId
                const isExpanded = expandedProjectIds.has(project.id)
                const projectDiscussions = discussions.filter(
                  (item) => item.project_id === project.id,
                )
                return (
                  <div key={project.id} role="listitem" className="project-node">
                    <button
                      type="button"
                      className={`project-item ${isActive ? 'active' : ''}`}
                      onClick={() => toggleProject(project)}
                      title={collapsed ? project.title : undefined}
                      aria-current={isActive ? 'true' : undefined}
                      aria-expanded={isExpanded}
                    >
                      <svg
                        className={`project-node-chevron sidebar-collapse-hide ${isExpanded ? 'is-open' : ''}`}
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="2"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        aria-hidden="true"
                      >
                        <path d="M9 6l6 6-6 6" />
                      </svg>
                      <svg className="project-folder-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                        <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" />
                      </svg>
                      <span className="nav-text sidebar-collapse-hide">{project.title}</span>
                    </button>
                    {isExpanded && !collapsed && (
                      <div className="project-session-list" role="list" aria-label={`${project.title}的聊天会话`}>
                        {projectDiscussions.length === 0 ? (
                          <p className="project-session-empty">暂无聊天会话</p>
                        ) : (
                          projectDiscussions.map((item) => {
                            const isMenuOpen = menuState?.id === item.id
                            const isRenaming = renamingId === item.id
                            return (
                              <div
                                key={item.id}
                                role="listitem"
                                className={`session-item project-session-item ${activeDiscussionId === item.id ? 'active' : ''}`}
                              >
                                {isRenaming ? (
                                  <form
                                    className="session-rename-form"
                                    onSubmit={(e) => {
                                      e.preventDefault()
                                      void handleRenameSubmit(item)
                                    }}
                                  >
                                    <input
                                      className="session-rename-input"
                                      value={renameDraft}
                                      autoFocus
                                      maxLength={80}
                                      disabled={busy}
                                      onChange={(e) => setRenameDraft(e.target.value)}
                                      onBlur={() => setRenamingId(null)}
                                      onKeyDown={(e) => {
                                        if (e.key === 'Escape') {
                                          e.preventDefault()
                                          setRenamingId(null)
                                        }
                                      }}
                                    />
                                  </form>
                                ) : (
                                  <button
                                    type="button"
                                    className="session-open"
                                    onClick={() => onOpenDiscussion(item.id, project.id)}
                                    title={item.title}
                                  >
                                    <span className="session-text">
                                      {item.title || '未命名讨论'}
                                    </span>
                                  </button>
                                )}
                                {!isRenaming && (
                                  <div className="session-actions">
                                    <button
                                      type="button"
                                      className={`session-more ${isMenuOpen ? 'is-open' : ''}`}
                                      aria-label="会话操作"
                                      aria-expanded={isMenuOpen}
                                      onClick={(e) => openSessionMenu(e, item)}
                                    >
                                      <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                                        <circle cx="5" cy="12" r="1.6" />
                                        <circle cx="12" cy="12" r="1.6" />
                                        <circle cx="19" cy="12" r="1.6" />
                                      </svg>
                                    </button>
                                  </div>
                                )}
                              </div>
                            )
                          })
                        )}
                      </div>
                    )}
                  </div>
                )
              })
            )}
          </div>
        )}
        {projectError && <p className="project-switcher-error sidebar-collapse-hide">{projectError}</p>}
      </div>

      <div className="rail-section rail-secondary">
        <button
          className="nav-item"
          onClick={onSettings}
          title={collapsed ? '设置' : undefined}
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3h.1a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8v.1a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <span className="nav-text sidebar-collapse-hide">设置</span>
        </button>
      </div>

      {createPortal(
        <>
          <ConfirmDialog
            open={Boolean(pendingDelete)}
            title="删除会话"
            description={`确定删除「${pendingDelete?.title || '未命名讨论'}」吗？聊天记录将一并移除，不可恢复。`}
            confirmLabel={busy ? '删除中…' : '删除'}
            tone="danger"
            onConfirm={() => void handleDeleteConfirm()}
            onCancel={() => {
              if (!busy) setPendingDelete(null)
            }}
          />
          <ConfirmDialog
            open={Boolean(pendingRemoveProject)}
            title="移除项目"
            description={`移除项目「${pendingRemoveProject?.title ?? ''}」？真实文件夹和产物不会被删除。`}
            confirmLabel="移除"
            tone="danger"
            onConfirm={() => void confirmRemoveProject()}
            onCancel={() => setPendingRemoveProject(null)}
          />
        </>,
        document.body,
      )}

      {menuState &&
        createPortal(
          <div
            className="session-menu"
            role="menu"
            style={{ top: menuState.top, left: menuState.left }}
            onClick={(e) => e.stopPropagation()}
          >
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                const item = discussions.find((d) => d.id === menuState.id)
                if (!item) return
                setRenameDraft(item.title || '')
                setRenamingId(item.id)
                setMenuState(null)
              }}
            >
              重命名
            </button>
            <button
              type="button"
              role="menuitem"
              className="danger"
              onClick={() => {
                const item = discussions.find((d) => d.id === menuState.id)
                if (!item) return
                setPendingDelete(item)
                setMenuState(null)
              }}
            >
              删除
            </button>
          </div>,
          document.body,
        )}
    </aside>
  )
}

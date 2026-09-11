import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'

import {
  deleteBrainstormDiscussion,
  fetchBrainstormDiscussions,
  renameBrainstormDiscussion,
  type BrainstormDiscussionSummary,
} from '../api'

type Props = {
  section: string
  collapsed: boolean
  activeDiscussionId: string | null
  onHome: () => void
  onModeling: () => void
  onBrainstorm: () => void
  onOpenDiscussion: (id: string) => void
  onApi: () => void
  onDocs: () => void
  onSettings: () => void
  onDiscussionDeleted?: (id: string) => void
}

const LOGO = (
  <div className="sidebar-brand-mark" aria-hidden="true">
    DA
  </div>
)

const CHAT_ICON = (
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
    <path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z" />
  </svg>
)

function formatRelativeTime(iso: string) {
  const time = new Date(iso).getTime()
  if (Number.isNaN(time)) return ''
  const diff = Date.now() - time
  if (diff < 60_000) return '刚刚'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h`
  return `${Math.floor(diff / 86_400_000)}d`
}

export default function Sidebar({
  section,
  collapsed,
  activeDiscussionId,
  onHome,
  onModeling,
  onBrainstorm,
  onOpenDiscussion,
  onApi,
  onDocs,
  onSettings,
  onDiscussionDeleted,
}: Props) {
  const [discussions, setDiscussions] = useState<BrainstormDiscussionSummary[]>([])
  const [menuState, setMenuState] = useState<{
    id: string
    top: number
    left: number
  } | null>(null)
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renameDraft, setRenameDraft] = useState('')
  const [pendingDelete, setPendingDelete] = useState<BrainstormDiscussionSummary | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    void fetchBrainstormDiscussions()
      .then(setDiscussions)
      .catch(() => setDiscussions([]))
  }, [section, activeDiscussionId])

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

  async function refresh() {
    try {
      setDiscussions(await fetchBrainstormDiscussions())
    } catch {
      /* keep current list on failure */
    }
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

  return (
    <aside className={`sidebar ${collapsed ? 'sidebar--collapsed' : ''}`}>
      <div className="sidebar-top">
        <div className="sidebar-logo" onClick={onHome} title="DAAgent 数据分析智能体">
          {LOGO}
          <div className="logo-text sidebar-collapse-hide">
            <span className="logo-name sidebar-brand">DAAgent</span>
            <span className="logo-sub sidebar-sub">数据分析智能体</span>
          </div>
        </div>
      </div>

      <div className="rail-section">
        <div className="rail-label sidebar-collapse-hide">工作区</div>
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
      </div>

      <div className="rail-section rail-recent">
        <div className="rail-label sidebar-collapse-hide">最近</div>
        {discussions.length === 0 ? (
          <p className="rail-recent-empty sidebar-collapse-hide">暂无聊天记录</p>
        ) : (
          discussions.map((item) => {
            const isMenuOpen = menuState?.id === item.id
            const isRenaming = renamingId === item.id
            return (
              <div
                key={item.id}
                className={`session-item ${activeDiscussionId === item.id ? 'active' : ''}`}
              >
                {isRenaming ? (
                  <form
                    className="session-rename-form sidebar-collapse-hide"
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
                    onClick={() => onOpenDiscussion(item.id)}
                    title={item.title}
                  >
                    {CHAT_ICON}
                    <span className="session-text sidebar-collapse-hide">
                      {item.title || '未命名讨论'}
                    </span>
                    <small className="sidebar-collapse-hide">
                      {formatRelativeTime(item.updated_at)}
                    </small>
                  </button>
                )}

                {!isRenaming && !collapsed && (
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

      <div className="rail-section rail-secondary">
        <button
          className={`nav-item ${section === 'api' ? 'active' : ''}`}
          onClick={onApi}
          title={collapsed ? 'API 管理' : undefined}
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
            <circle cx="12" cy="12" r="9" />
            <path d="M14.5 9.5 10 12.5 14 15.5" strokeLinecap="round" strokeLinejoin="round" />
            <path d="M12 3v3M12 18v3M3 12h3M18 12h3" strokeLinecap="round" />
          </svg>
          <span className="nav-text sidebar-collapse-hide">API 管理</span>
        </button>
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
        <button
          className={`nav-item ${section === 'docs' ? 'active' : ''}`}
          onClick={onDocs}
          title={collapsed ? '项目文档' : undefined}
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
            <path d="M4 4h11l5 5v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1z" />
            <path d="M14 4v5h5" strokeLinecap="round" strokeLinejoin="round" />
            <path d="M8 13h8M8 17h8" strokeLinecap="round" />
          </svg>
          <span className="nav-text sidebar-collapse-hide">项目文档</span>
        </button>
      </div>

      <div className="rail-foot">
        <div className="agent-status" title={collapsed ? 'Agent Online' : undefined}>
          <span className="pulse" />
          <span className="sidebar-collapse-hide">Agent Online</span>
        </div>
      </div>

      {pendingDelete &&
        createPortal(
          <div className="modal-mask" onClick={() => setPendingDelete(null)} role="presentation">
            <div
              className="modal"
              role="dialog"
              aria-modal="true"
              onClick={(e) => e.stopPropagation()}
            >
              <h3>删除会话</h3>
              <p className="muted">
                确定删除「{pendingDelete.title || '未命名讨论'}」吗？聊天记录将一并移除，不可恢复。
              </p>
              <div className="modal-actions">
                <button
                  type="button"
                  className="btn-ghost"
                  disabled={busy}
                  onClick={() => setPendingDelete(null)}
                >
                  取消
                </button>
                <button
                  type="button"
                  className="btn-danger"
                  disabled={busy}
                  onClick={() => void handleDeleteConfirm()}
                >
                  {busy ? '删除中…' : '删除'}
                </button>
              </div>
            </div>
          </div>,
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

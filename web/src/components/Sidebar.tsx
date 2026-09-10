import { useEffect, useState } from 'react'

import { fetchBrainstormDiscussions, type BrainstormDiscussionSummary } from '../api'

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
}: Props) {
  const [discussions, setDiscussions] = useState<BrainstormDiscussionSummary[]>([])

  useEffect(() => {
    void fetchBrainstormDiscussions()
      .then(setDiscussions)
      .catch(() => setDiscussions([]))
  }, [section, activeDiscussionId])

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
          discussions.map((item) => (
            <button
              key={item.id}
              className={`session-item ${activeDiscussionId === item.id ? 'active' : ''}`}
              onClick={() => onOpenDiscussion(item.id)}
              title={item.title}
            >
              {CHAT_ICON}
              <span className="session-text sidebar-collapse-hide">{item.title || '未命名讨论'}</span>
              <small className="sidebar-collapse-hide">{formatRelativeTime(item.updated_at)}</small>
            </button>
          ))
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
    </aside>
  )
}

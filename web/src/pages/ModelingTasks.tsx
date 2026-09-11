type Props = {
  onNew: () => void
  onHistory: () => void
}

export default function ModelingTasks({ onNew, onHistory }: Props) {
  return (
    <div className="page modeling-tasks-page">
      <div className="page-head modeling-tasks-head">
        <div>
          <h1 className="page-title">建模任务</h1>
          <p className="page-sub">开始新的聊天（灵感讨论或启动建模），或继续查看和管理已有任务。</p>
        </div>
      </div>

      <div className="modeling-task-grid">
        <button className="modeling-task-card" onClick={onNew}>
          <span className="modeling-task-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
              <path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z" />
            </svg>
          </span>
          <span className="modeling-task-copy">
            <strong>新建聊天</strong>
            <span>基于知识库灵感讨论，或提交问题与资料启动建模流程。</span>
          </span>
          <span className="modeling-task-arrow" aria-hidden="true">→</span>
        </button>

        <button className="modeling-task-card" onClick={onHistory}>
          <span className="modeling-task-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M3 3v18h18" />
              <path d="M7 14l4-4 3 3 5-6" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </span>
          <span className="modeling-task-copy">
            <strong>历史任务</strong>
            <span>查看、继续或管理过去的建模任务与产物。</span>
          </span>
          <span className="modeling-task-arrow" aria-hidden="true">→</span>
        </button>
      </div>
    </div>
  )
}

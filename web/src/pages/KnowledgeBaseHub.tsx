type Props = {
  onUpdate: () => void
  onHistory: () => void
}

export default function KnowledgeBaseHub({ onUpdate, onHistory }: Props) {
  return (
    <div className="page modeling-tasks-page brainstorm-hub-page brainstorm-main-hub-page">
      <div className="page-head modeling-tasks-head">
        <div>
          <h1 className="page-title">知识库维护</h1>
          <p className="page-sub">上传任务与历史文件记录分开：更新页只做本次上传，详细记录见统计。</p>
        </div>
      </div>

      <div className="modeling-task-grid">
        <button className="modeling-task-card" onClick={onUpdate}>
          <span className="modeling-task-icon" aria-hidden="true">＋</span>
          <span className="modeling-task-copy">
            <strong>知识库更新</strong>
            <span>完成一次上传与转换任务，打开时列表为空。</span>
          </span>
          <span className="modeling-task-arrow" aria-hidden="true">→</span>
        </button>

        <button className="modeling-task-card" onClick={onHistory}>
          <span className="modeling-task-icon" aria-hidden="true">◷</span>
          <span className="modeling-task-copy">
            <strong>知识库统计</strong>
            <span>查看各知识库的详细文件记录，并管理知识库。</span>
          </span>
          <span className="modeling-task-arrow" aria-hidden="true">→</span>
        </button>
      </div>
    </div>
  )
}

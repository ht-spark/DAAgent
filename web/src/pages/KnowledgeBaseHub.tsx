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
          <p className="page-sub">归集参考资料，在分析任务中检索、引用和复用。</p>
        </div>
      </div>

      <div className="modeling-task-grid">
        <button className="modeling-task-card" onClick={onUpdate}>
          <span className="modeling-task-icon" aria-hidden="true">＋</span>
          <span className="modeling-task-copy">
            <strong>添加资料</strong>
            <span>创建知识库，上传文档并转换为可检索的参考资料。</span>
          </span>
          <span className="modeling-task-arrow" aria-hidden="true">→</span>
        </button>

        <button className="modeling-task-card" onClick={onHistory}>
          <span className="modeling-task-icon" aria-hidden="true">◷</span>
          <span className="modeling-task-copy">
            <strong>管理资料</strong>
            <span>查看已收录文档、处理状态与知识库统计。</span>
          </span>
          <span className="modeling-task-arrow" aria-hidden="true">→</span>
        </button>
      </div>
    </div>
  )
}

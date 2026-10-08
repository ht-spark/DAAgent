import Result from './Result'
import NewChat from './NewChat'

type TaskStep = 'submit' | 'progress' | 'result'

type Props = {
  task: { step: TaskStep; runId: string | null }
  setTask: (t: { step: TaskStep; runId: string | null }) => void
  onHistory: () => void
  discussionId: string | null
  projectId: string | null
  onDiscussionId: (id: string) => void
  /** 跳转到「技能」管理页（对话「+」菜单的“管理技能”） */
  onManageSkills?: () => void
}

export default function NewTask({
  task,
  setTask,
  onHistory,
  discussionId,
  projectId,
  onDiscussionId,
  onManageSkills,
}: Props) {
  // 结果页保留原有查看体验
  if (task.step === 'result' && task.runId) {
    return (
      <div className="page page-fill">
        <Result
          runId={task.runId}
          onBack={() => setTask({ step: 'submit', runId: null })}
          onHistory={onHistory}
        />
      </div>
    )
  }

  // 新建聊天统一进入数分 Agent；历史建模任务仍可继续查看。
  return (
    <NewChat
      discussionId={discussionId}
      projectId={projectId}
      onDiscussionId={onDiscussionId}
      resumeRunId={task.step === 'progress' ? task.runId : null}
      onViewResult={(id) => setTask({ step: 'result', runId: id })}
      onManageSkills={onManageSkills}
    />
  )
}

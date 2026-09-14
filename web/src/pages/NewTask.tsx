import Result from './Result'
import NewChat from './NewChat'

type TaskStep = 'submit' | 'progress' | 'result'

type Props = {
  task: { step: TaskStep; runId: string | null }
  setTask: (t: { step: TaskStep; runId: string | null }) => void
  onHistory: () => void
  discussionId: string | null
  onDiscussionId: (id: string) => void
}

export default function NewTask({
  task,
  setTask,
  onHistory,
  discussionId,
  onDiscussionId,
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

  // 新建聊天：灵感迸发 RAG 对话 + 可切换到建模任务
  return (
    <NewChat
      discussionId={discussionId}
      onDiscussionId={onDiscussionId}
      resumeRunId={task.step === 'progress' ? task.runId : null}
      onViewResult={(id) => setTask({ step: 'result', runId: id })}
    />
  )
}

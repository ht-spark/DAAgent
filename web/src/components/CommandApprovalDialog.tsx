import { useEffect } from 'react'
import type { TerminalType } from '../settings'

type Props = {
  command: string
  workspaceRoot: string
  shell: TerminalType
  timeoutSeconds?: number | null
  onApprove: () => void
  onReject: () => void
}

/**
 * Agent 终端命令的人工批准卡片：贴在聊天输入框上方，
 * 与 WorkBuddy 等 agent 工具一致，不打断会话上下文。
 */
export default function CommandApprovalDialog({
  command,
  workspaceRoot,
  shell,
  timeoutSeconds,
  onApprove,
  onReject,
}: Props) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onReject()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onReject])

  return (
    <div
      className="rag-approval-card"
      role="dialog"
      aria-labelledby="command-approval-title"
      aria-describedby="command-approval-desc"
    >
      <div className="rag-approval-head">
        <div className="rag-approval-head-copy">
          <p className="rag-approval-kicker">等待人工批准</p>
          <h3 id="command-approval-title">Agent 请求操作终端</h3>
          <p id="command-approval-desc" className="rag-approval-lead">
            请检查命令与项目路径，确认后再决定是否批准执行。
          </p>
        </div>
        <span className="rag-approval-badge" aria-hidden="true">待确认</span>
      </div>

      <div className="rag-approval-command" aria-label="待批准命令">
        <span className="rag-approval-prompt" aria-hidden="true">$</span>
        <code>{command}</code>
      </div>

      <dl className="rag-approval-meta">
        <div>
          <dt>项目</dt>
          <dd>{workspaceRoot}</dd>
        </div>
        <div>
          <dt>Shell</dt>
          <dd>{shell === 'git-bash' ? 'Git Bash' : shell === 'powershell' ? 'PowerShell' : 'Bash'}</dd>
        </div>
        {timeoutSeconds ? (
          <div>
            <dt>超时</dt>
            <dd>{timeoutSeconds} 秒</dd>
          </div>
        ) : null}
      </dl>

      <div className="rag-approval-foot">
        <p className="rag-approval-warning">
          该命令会由真实 Shell 解释，可能访问系统、安装依赖、执行代码，并创建、修改或删除文件。每次执行只批准当前这一条命令。
        </p>
        <div className="rag-approval-actions">
          <button type="button" className="rag-approval-reject" onClick={onReject}>
            拒绝
          </button>
          <button type="button" className="rag-approval-approve" onClick={onApprove} autoFocus>
            批准运行
          </button>
        </div>
      </div>
    </div>
  )
}

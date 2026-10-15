import { memo, useEffect, useId, useMemo, useRef, useState } from 'react'

import { buildAgentTraceRows, type AgentTraceRow } from '../agentTraceRows'
import {
  agentRunFailed,
  hasActivityEvents,
  isActivityEvent,
  summarizeTodoObjective,
  type AgentPlan,
  type TodoStateMap,
  type TodoStatus,
} from '../agentTasks'
import type { AgentTraceEvent } from '../api'
import MarkdownContent from './MarkdownContent'
import { TraceIcon } from './TraceIcon'

type Props = {
  trace: AgentTraceEvent[]
  /** 该轨迹是否仍在运行：驱动最后一行与整体展开状态 */
  running: boolean
  /** 当前执行计划与任务状态：只用于渲染行尾的当前任务上下文 */
  plan?: AgentPlan
  todoState?: TodoStateMap
}

/**
 * 单行轨迹：图标 + 动作 · 摘要 + 行尾次要信息。
 *
 * 工具摘要保持单行；思考行默认展开并完整展示流式 Markdown 内容。
 */
const TraceRowItem = memo(function TraceRowItem({ row }: { row: AgentTraceRow }) {
  const thinkingStartedAtRef = useRef<number | null>(
    row.kind === 'thinking' && row.status === 'running' ? Date.now() : null,
  )
  const [thinkingSeconds, setThinkingSeconds] = useState(0)
  // 思考行默认展开，让流式内容一进来就可见；仍允许用户手动收起
  const [thinkingExpanded, setThinkingExpanded] = useState(true)

  useEffect(() => {
    if (row.kind !== 'thinking') return undefined
    if (row.status !== 'running') {
      const startedAt = thinkingStartedAtRef.current
      if (startedAt !== null) {
        setThinkingSeconds(Math.max(0, Math.round((Date.now() - startedAt) / 1000)))
      }
      return undefined
    }
    if (thinkingStartedAtRef.current === null) thinkingStartedAtRef.current = Date.now()
    const updateElapsed = () => {
      const startedAt = thinkingStartedAtRef.current
      if (startedAt !== null) {
        setThinkingSeconds(Math.max(0, Math.round((Date.now() - startedAt) / 1000)))
      }
    }
    updateElapsed()
    const timer = window.setInterval(updateElapsed, 1000)
    return () => window.clearInterval(timer)
  }, [row.kind, row.status])

  if (row.kind === 'thinking') {
    const label = row.status === 'running' ? '思考中' : '已思考'
    const duration = thinkingSeconds > 0
      ? `${row.status === 'running' ? '已用时' : '用时'} ${thinkingSeconds} 秒`
      : ''
    return (
      <details
        className="agent-trace-details agent-thinking-details"
        /* React 没有 defaultOpen 这个属性：之前写的 defaultOpen 会被渲染成无效的
           defaultopen 特性并被浏览器忽略，导致思考行其实并没有默认展开。
           这里改成受控 open，首帧就是展开态，同时保留手动收起的能力。 */
        open={thinkingExpanded}
        onToggle={(event) => setThinkingExpanded(event.currentTarget.open)}
      >
        <summary title="展开或收起完整思考">
          <TraceIcon name={row.icon} />
          <span className="agent-trace-action">{label}</span>
          {duration && <span className="agent-trace-meta">（{duration}）</span>}
          <span className="agent-trace-chevron" aria-hidden="true" />
        </summary>
        <div className="agent-thinking-body">
          <MarkdownContent content={row.content ?? row.summary ?? ''} />
        </div>
      </details>
    )
  }

  const body = (
    <>
      <TraceIcon name={row.icon} />
      <span className="agent-trace-action">{row.action}</span>
      {row.summary && (
        <>
          <span className="agent-trace-sep" aria-hidden="true">·</span>
          <span className="agent-trace-summary" title={row.summary}>{row.summary}</span>
        </>
      )}
      {row.meta && <span className="agent-trace-meta">{row.meta}</span>}
    </>
  )

  if (!row.details?.length) {
    return <div className="agent-trace-line">{body}</div>
  }

  return (
    <details className="agent-trace-details">
      <summary title={row.detailLabel ?? '展开查看详情'}>
        {body}
        <span className="agent-trace-chevron" aria-hidden="true" />
      </summary>
      <div className="agent-trace-detail-body">
        {row.details.map((detail, index) => (
          <div className="agent-trace-detail-block" key={`${detail.label}-${index}`}>
            <p className="agent-trace-detail-label">{detail.label}</p>
            <pre>{detail.text}</pre>
          </div>
        ))}
      </div>
    </details>
  )
})

/**
 * Agent 工作轨迹：一行一步的摘要列表（参考 DeepSeek Harness 的轨迹呈现）。
 *
 * 运行中始终展开；结束后自动收起为一行「已完成，用时 xx 秒」，点击可重新展开。
 * 挂载时就已经结束的轨迹（历史讨论、页面刷新）同样按已收起展示，避免长轨迹刷屏。
 */
const AgentTrace = memo(function AgentTrace({ trace, running, plan, todoState }: Props) {
  // isLive：本组件是否亲历了这次运行 —— 只有亲历运行才可信地统计用时
  const [isLive] = useState(running)
  const [collapsed, setCollapsed] = useState(() => !running && trace.some(isActivityEvent))
  const startedAtRef = useRef<number | null>(null)
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const rows = useMemo(() => buildAgentTraceRows(trace, { running }), [trace, running])
  const failed = useMemo(() => agentRunFailed(trace), [trace])
  const listId = useId()

  useEffect(() => {
    if (running) {
      setCollapsed(false)
      return
    }
    // 结束瞬间结算用时；历史轨迹（挂载时已结束）不产生误导性的用时
    const startedAt = startedAtRef.current
    if (isLive && startedAt !== null) {
      setElapsedSeconds(Math.max(0, Math.round((Date.now() - startedAt) / 1000)))
    }
    setCollapsed(true)
  }, [isLive, running])

  // 首帧后开始计时：只有真正运行起来的轨迹才有可信用时
  useEffect(() => {
    if (running && startedAtRef.current === null) startedAtRef.current = Date.now()
  }, [running])

  // 最后开始（或失败）的任务：为轨迹提供“当前任务”上下文
  let activeTodo: { objective: string; status: TodoStatus } | undefined
  if (plan) {
    for (let index = plan.todos.length - 1; index >= 0; index -= 1) {
      const status = todoState?.[plan.todos[index].id]
      if (status === 'in_progress' || status === 'failed') {
        activeTodo = { objective: plan.todos[index].objective, status }
        break
      }
    }
  }

  if (!rows.length) return null

  return (
    <div className={`agent-trace${running ? ' is-running' : ''}${collapsed ? ' is-collapsed' : ''}`}>
      {!collapsed && (
        <ol className="agent-trace-list" id={listId}>
          {rows.map((row) => (
            <li className={`agent-trace-row is-${row.status}`} key={row.id}>
              <TraceRowItem row={row} />
            </li>
          ))}
        </ol>
      )}
      {plan && activeTodo && (
        // 执行计划详情在输入框上方的任务卡片里，这里只保留一句任务上下文
        <p className={`agent-trace-task is-${activeTodo.status}`}>
          {activeTodo.status === 'failed'
            ? `当前任务失败 · ${summarizeTodoObjective(activeTodo.objective)}`
            : `当前任务 · ${summarizeTodoObjective(activeTodo.objective)}`}
        </p>
      )}
      {hasActivityEvents(trace) && (
        <button
          type="button"
          className={`agent-steps-toggle${collapsed ? ' is-collapsed' : ''}`}
          onClick={() => setCollapsed((value) => !value)}
          aria-expanded={!collapsed}
          aria-controls={listId}
          title={collapsed ? '展开工作轨迹' : '收起工作轨迹'}
        >
          <span>
            {running
              ? '正在执行…'
              : failed
                ? elapsedSeconds > 0
                  ? `执行失败，用时 ${elapsedSeconds} 秒`
                  : '执行失败'
              : elapsedSeconds > 0
                ? `已完成，用时 ${elapsedSeconds} 秒`
                : '已完成'}
          </span>
          <span className="agent-steps-toggle-icon" aria-hidden="true" />
        </button>
      )}
    </div>
  )
})

export default AgentTrace

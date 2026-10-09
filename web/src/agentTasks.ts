/** agent 执行计划与任务状态的纯函数工具：供消息轨迹与输入框上方的任务卡片共用。 */

import type { AgentTraceEvent } from './api'

/** Agent 执行计划事件（最新一次规划的结果） */
export type AgentPlan = Extract<AgentTraceEvent, { type: 'agent_plan' }>

/** 单个任务的执行状态 */
export type TodoStatus = 'pending' | 'in_progress' | 'completed' | 'failed' | 'terminated'

/** 任务状态映射：任务 ID -> 执行状态 */
export type TodoStateMap = Record<string, TodoStatus>

/** 任务状态对应的中文标签 */
export const TODO_STATUS_LABELS: Record<TodoStatus, string> = {
  pending: '等待中',
  in_progress: '进行中',
  completed: '已完成',
  failed: '失败',
  terminated: '已终止',
}

const DEFAULT_TODO_SUMMARY_LENGTH = 34

/** 把任务目标压成列表可扫读的短摘要；完整目标仍由调用方放入 title。 */
export function summarizeTodoObjective(
  objective: string,
  maxLength = DEFAULT_TODO_SUMMARY_LENGTH,
): string {
  const normalized = objective.replace(/\s+/g, ' ').trim().replace(/[。；;]+$/, '')
  const characters = Array.from(normalized)
  if (characters.length <= maxLength) return normalized
  return `${characters.slice(0, maxLength).join('').trimEnd()}…`
}

/** 计划与任务生命周期事件：只在任务卡片中呈现，不出现在消息轨迹里 */
type PlanLifecycleEvent = Extract<
  AgentTraceEvent,
  { type: 'agent_plan' | 'agent_todo_started' | 'agent_todo_completed' | 'agent_todo_failed' }
>

/** 取出轨迹中最新的一次执行计划；没有规划时返回 undefined */
export function latestAgentPlan(trace: AgentTraceEvent[]): AgentPlan | undefined {
  return trace.reduce<AgentPlan | undefined>(
    (latest, event) => event.type === 'agent_plan' ? event : latest,
    undefined,
  )
}

/**
 * 由规划与轨迹事件推导每个任务的状态。
 *
 * 规划中的任务默认 pending；同一任务可能出现多次事件（例如失败后重试），
 * 因此按时间顺序覆盖写入，最终保留最后一次事件的状态。
 * 不属于当前计划的任务 ID（上一次规划或压缩后的残留事件）会被忽略，
 * 避免陈旧状态混入当前任务列表。
 *
 * @param plan 最新执行计划，缺省时返回空映射
 * @param trace 完整轨迹事件
 * @returns 任务 ID 到状态的映射
 */
export function deriveTodoStates(
  plan: AgentPlan | undefined,
  trace: AgentTraceEvent[],
  runTerminated = false,
): TodoStateMap {
  const states: TodoStateMap = {}
  if (!plan) return states
  plan.todos.forEach((todo) => { states[todo.id] = 'pending' })
  trace.forEach((event) => {
    if (event.type !== 'agent_todo_started'
      && event.type !== 'agent_todo_completed'
      && event.type !== 'agent_todo_failed') {
      return
    }
    if (!(event.todo_id in states)) return
    if (event.type === 'agent_todo_started') states[event.todo_id] = 'in_progress'
    if (event.type === 'agent_todo_completed') states[event.todo_id] = 'completed'
    if (event.type === 'agent_todo_failed') states[event.todo_id] = 'failed'
  })
  if (runTerminated) {
    Object.entries(states).forEach(([todoId, status]) => {
      if (status === 'pending' || status === 'in_progress') states[todoId] = 'terminated'
    })
  }
  return states
}

/** 统计各状态的任务数量，供任务卡片标题栏使用 */
export function countTodoStates(
  plan: AgentPlan | undefined,
  states: TodoStateMap,
): { pending: number; inProgress: number; completed: number; failed: number; terminated: number } {
  const counts = { pending: 0, inProgress: 0, completed: 0, failed: 0, terminated: 0 }
  plan?.todos.forEach((todo) => {
    const status = states[todo.id]
    if (status === 'completed') counts.completed += 1
    else if (status === 'failed') counts.failed += 1
    else if (status === 'terminated') counts.terminated += 1
    else if (status === 'in_progress') counts.inProgress += 1
    else counts.pending += 1
  })
  return counts
}

/** 过滤掉计划/任务生命周期事件，保留思考与工具轨迹 */
export function isActivityEvent(
  event: AgentTraceEvent,
): event is Exclude<AgentTraceEvent, PlanLifecycleEvent> {
  return event.type !== 'agent_plan'
    && event.type !== 'agent_todo_started'
    && event.type !== 'agent_todo_completed'
    && event.type !== 'agent_todo_failed'
}

/** 工作轨迹只要含有活动事件，运行中与完成后都应保留展开入口 */
export function hasActivityEvents(trace: AgentTraceEvent[]): boolean {
  return trace.some(isActivityEvent)
}

/** 判断整轮 Agent 是否以失败结束，区别于单个 Todo 的可恢复失败。 */
export function agentRunFailed(trace: AgentTraceEvent[]): boolean {
  return trace.some((event) => event.type === 'agent_failed')
}

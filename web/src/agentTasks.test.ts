/**
 * agentTasks 纯函数的行为测试（Node 内置测试运行器）。
 *
 * 运行方式（依赖 Node 22+ 的类型剥离与 esbuild 的路径补全）：
 *   node --experimental-strip-types --experimental-transform-types --test src/agentTasks.test.ts
 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  countTodoStates,
  deriveTodoStates,
  hasActivityEvents,
  isActivityEvent,
  latestAgentPlan,
  summarizeTodoObjective,
  type AgentPlan,
} from './agentTasks.ts'
import type { AgentTraceEvent } from './api.ts'

/** 构造一个包含两个任务的计划事件 */
function planEvent(ids: string[] = ['t1', 't2']): AgentPlan {
  return {
    type: 'agent_plan',
    objective: '交付一次完整建模',
    todos: ids.map((id) => ({
      id,
      objective: `任务 ${id}`,
      depends_on: [],
      expected_outputs: [],
    })),
  }
}

test('latestAgentPlan 取最后一次规划', () => {
  const first = planEvent(['a'])
  const second = planEvent(['b'])
  const trace: AgentTraceEvent[] = [
    first,
    { type: 'agent_action', thought: '', tool_name: 'read_file' },
    second,
  ]
  assert.equal(latestAgentPlan(trace), second)
})

test('latestAgentPlan 无规划时返回 undefined', () => {
  assert.equal(latestAgentPlan([{ type: 'agent_started', run_id: 'r1', files: [] }]), undefined)
})

test('deriveTodoStates 默认全部为 pending', () => {
  const states = deriveTodoStates(planEvent(), [])
  assert.deepEqual(states, { t1: 'pending', t2: 'pending' })
})

test('deriveTodoStates 依事件推进状态，未出现的任务保持 pending', () => {
  const plan = planEvent(['t1', 't2', 't3'])
  const trace: AgentTraceEvent[] = [
    plan,
    { type: 'agent_todo_started', todo_id: 't1', objective: '任务 t1' },
    { type: 'agent_todo_completed', todo_id: 't1', result_summary: 'ok' },
    { type: 'agent_todo_started', todo_id: 't2', objective: '任务 t2' },
  ]
  assert.deepEqual(deriveTodoStates(plan, trace), {
    t1: 'completed',
    t2: 'in_progress',
    t3: 'pending',
  })
})

test('deriveTodoStates 以最后一次事件为准（重试场景）', () => {
  const plan = planEvent(['t1'])
  const trace: AgentTraceEvent[] = [
    plan,
    { type: 'agent_todo_started', todo_id: 't1', objective: '任务 t1' },
    { type: 'agent_todo_failed', todo_id: 't1', error: 'boom' },
    { type: 'agent_todo_started', todo_id: 't1', objective: '任务 t1' },
    { type: 'agent_todo_completed', todo_id: 't1', result_summary: 'ok' },
  ]
  assert.equal(deriveTodoStates(plan, trace).t1, 'completed')
})

test('deriveTodoStates 忽略不属于当前计划的陈旧任务 ID', () => {
  const previous = planEvent(['old'])
  const current = planEvent(['new'])
  const trace: AgentTraceEvent[] = [
    previous,
    { type: 'agent_todo_completed', todo_id: 'old', result_summary: 'ok' },
    current,
  ]
  const states = deriveTodoStates(current, trace)
  assert.deepEqual(states, { new: 'pending' })
  // 陈旧 ID 不会污染统计结果
  assert.deepEqual(countTodoStates(current, states), {
    pending: 1,
    inProgress: 0,
    completed: 0,
    failed: 0,
    terminated: 0,
  })
})

test('deriveTodoStates 无计划时返回空映射', () => {
  assert.deepEqual(deriveTodoStates(undefined, []), {})
})

test('countTodoStates 汇总各状态数量', () => {
  const plan = planEvent(['t1', 't2', 't3', 't4'])
  const states = deriveTodoStates(plan, [
    { type: 'agent_todo_completed', todo_id: 't1', result_summary: 'ok' },
    { type: 'agent_todo_failed', todo_id: 't2', error: 'boom' },
    { type: 'agent_todo_started', todo_id: 't3', objective: '任务 t3' },
  ])
  assert.deepEqual(countTodoStates(plan, states), {
    pending: 1,
    inProgress: 1,
    completed: 1,
    failed: 1,
    terminated: 0,
  })
})

test('运行终止时将进行中与等待中的任务同步标记为已终止', () => {
  const plan = planEvent(['t1', 't2', 't3'])
  const trace: AgentTraceEvent[] = [
    plan,
    { type: 'agent_todo_completed', todo_id: 't1', result_summary: 'ok' },
    { type: 'agent_todo_started', todo_id: 't2', objective: '任务 t2' },
  ]

  const states = deriveTodoStates(plan, trace, true)

  assert.deepEqual(states, { t1: 'completed', t2: 'terminated', t3: 'terminated' })
  assert.deepEqual(countTodoStates(plan, states), {
    pending: 0,
    inProgress: 0,
    completed: 1,
    failed: 0,
    terminated: 2,
  })
})

test('summarizeTodoObjective 压缩空白并限制任务摘要长度', () => {
  const objective = '  基于画像结果，  抽样查看 data.csv 的真实记录以核验关键字段取值、异常样本与候选键的唯一性。  '

  assert.equal(
    summarizeTodoObjective(objective),
    '基于画像结果， 抽样查看 data.csv 的真实记录以核验关键字段…',
  )
  assert.equal(summarizeTodoObjective('生成数据画像。'), '生成数据画像')
})

test('isActivityEvent 过滤计划与任务生命周期事件', () => {
  const events: AgentTraceEvent[] = [
    planEvent(['t1']),
    { type: 'agent_todo_started', todo_id: 't1', objective: '任务 t1' },
    { type: 'agent_todo_completed', todo_id: 't1', result_summary: 'ok' },
    { type: 'agent_todo_failed', todo_id: 't1', error: 'boom' },
    { type: 'agent_action', thought: '', tool_name: 'read_file' },
    { type: 'agent_observation', tool_name: 'read_file', success: true, duration_ms: 12 },
  ]
  assert.deepEqual(
    events.filter(isActivityEvent).map((event) => event.type),
    ['agent_action', 'agent_observation'],
  )
})

test('已完成的实时轨迹仍保留展开入口', () => {
  const trace: AgentTraceEvent[] = [
    { type: 'agent_started', run_id: 'r1', files: ['paper.pdf'] },
    { type: 'agent_action', thought: '读取论文', tool_name: 'markitdown' },
    { type: 'agent_observation', tool_name: 'markitdown', success: true, duration_ms: 1200 },
  ]

  assert.equal(hasActivityEvents(trace), true)
  assert.equal(hasActivityEvents([planEvent(['t1'])]), false)
})

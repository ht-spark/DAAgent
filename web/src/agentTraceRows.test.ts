/**
 * agentTraceRows 的行为测试：事件流压成单行摘要列表的合并规则。
 *
 * 运行方式（Node 22+ 类型剥离）：
 *   node --experimental-strip-types src/agentTraceRows.test.ts
 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

import { buildAgentTraceRows, formatDuration, summarizeText } from './agentTraceRows.ts'
import type { AgentTraceEvent } from './api.ts'

function started(files: string[] = []): AgentTraceEvent {
  return { type: 'agent_started', run_id: 'r1', files }
}

function plan(todos: number): AgentTraceEvent {
  return {
    type: 'agent_plan',
    objective: '完成一次完整建模',
    reasoning_summary: '先看数据，再建模。',
    todos: Array.from({ length: todos }, (_, index) => ({
      id: `t${index + 1}`,
      objective: `任务 ${index + 1}`,
      depends_on: [],
      expected_outputs: [],
    })),
  }
}

function action(
  tool: string,
  args: Record<string, string> = {},
  extra: Partial<Extract<AgentTraceEvent, { type: 'agent_action' }>> = {},
): AgentTraceEvent {
  return { type: 'agent_action', thought: '先确认数据规模', tool_name: tool, arguments: args, ...extra }
}

function observation(
  tool: string,
  extra: Partial<Extract<AgentTraceEvent, { type: 'agent_observation' }>> = {},
): AgentTraceEvent {
  return { type: 'agent_observation', tool_name: tool, success: true, duration_ms: 1200, ...extra }
}

test('summarizeText 只保留首行并按长度截断', () => {
  assert.equal(summarizeText('第一行\n第二行'), '第一行')
  assert.equal(summarizeText('  a   b  '), 'a b')
  assert.equal(summarizeText('x'.repeat(200)).length, 161)
})

test('formatDuration 生成紧凑耗时', () => {
  assert.equal(formatDuration(0), '')
  assert.equal(formatDuration(820), '820 ms')
  assert.equal(formatDuration(1200), '1.2 s')
  assert.equal(formatDuration(65_000), '1 分 5 秒')
})

test('计划行摘要带任务数与可展开的任务清单', () => {
  const rows = buildAgentTraceRows([started(['a.csv']), plan(2)])

  assert.equal(rows[0].action, '开始')
  assert.equal(rows[0].summary, '已接收 1 个附件')

  assert.equal(rows[1].action, '规划')
  assert.equal(rows[1].summary, '完成一次完整建模')
  assert.equal(rows[1].meta, '2 个任务')
  const labels = rows[1].details?.map((detail) => detail.label) ?? []
  assert.deepEqual(labels, ['规划说明', '任务清单'])
  assert.match(rows[1].details?.[1].text ?? '', /1\. 任务 1/)
})

test('一次工具调用只产生「思考行 + 工具行」，观测并入工具行', () => {
  const rows = buildAgentTraceRows([
    action('read_workspace_file', { relative_path: 'web/src/App.tsx' }),
    observation('read_workspace_file', { details: '文件内容' }),
  ])

  assert.equal(rows.length, 2)
  assert.equal(rows[0].action, '思考')
  assert.equal(rows[0].summary, '先确认数据规模')
  assert.equal(rows[1].action, '读取')
  assert.equal(rows[1].summary, 'web/src/App.tsx')
  assert.equal(rows[1].status, 'success')
  assert.equal(rows[1].meta, '1.2 s')
  assert.equal(rows[1].details?.[0].text, '文件内容')
})

test('工具行按白名单参数与代码注释取摘要', () => {
  const rows = buildAgentTraceRows([
    action('run_project_command', { command: 'python -m pytest' }),
    observation('run_project_command'),
    action('execute_python', {}, { code: '# 计算相关系数\nresult = 1' }),
    observation('execute_python'),
  ])

  assert.equal(rows[1].action, '运行命令')
  assert.equal(rows[1].summary, 'python -m pytest')
  assert.equal(rows[3].action, '运行 Python')
  assert.equal(rows[3].summary, '计算相关系数')
})

test('未登记的工具回退到工具名，并保留代码详情', () => {
  const rows = buildAgentTraceRows([
    action('brand_new_tool', {}, { code: 'print(1)' }),
  ])

  assert.equal(rows[1].action, 'brand_new_tool')
  assert.equal(rows[1].summary, undefined)
  assert.equal(rows[1].details?.[0].label, '本轮 Python 代码')
})

test('批准与拒绝并入同一次工具调用', () => {
  const rows = buildAgentTraceRows([
    action('run_project_command', { command: 'git status' }),
    { type: 'agent_command_approval_required', approval_id: 'a1', command: 'git status', workspace_root: 'D:/w' },
    { type: 'agent_command_approval_decision', approval_id: 'a1', approved: true },
    observation('run_project_command', { details: '$ git status\nclean' }),
  ])

  assert.equal(rows.length, 2)
  assert.equal(rows[1].status, 'success')
  assert.equal(rows[1].meta, '1.2 s')
})

test('等待批准的失败工具行标记为失败并保留错误摘要', () => {
  const rows = buildAgentTraceRows([
    action('run_project_command', { command: 'git push' }),
    { type: 'agent_command_approval_required', approval_id: 'a1', command: 'git push', workspace_root: 'D:/w' },
    { type: 'agent_command_approval_decision', approval_id: 'a1', approved: false },
  ])

  assert.equal(rows[1].status, 'failed')
  assert.equal(rows[1].meta, '已拒绝')
})

test('运行中：未收到结果的工具行保持进行中，否则补一行思考占位', () => {
  const pending = buildAgentTraceRows(
    [action('profile_data'), ],
    { running: true },
  )
  assert.equal(pending[1].status, 'running')
  assert.equal(pending[1].meta, '执行中')

  const idle = buildAgentTraceRows([started()], { running: true })
  assert.equal(idle[idle.length - 1]?.status, 'running')
  assert.equal(idle[idle.length - 1]?.summary, '正在继续分析…')
})

test('工具失败时保留动作摘要，错误信息进详情', () => {
  const rows = buildAgentTraceRows([
    action('search_workspace_text', { query: '时间序列' }),
    observation('search_workspace_text', {
      success: false,
      error: '搜索路径不存在：src/features\n请先确认项目目录结构。',
    }),
  ])

  assert.equal(rows[1].summary, '时间序列')
  assert.equal(rows[1].status, 'failed')
  assert.equal(rows[1].meta, '失败 · 1.2 s')
  assert.deepEqual(rows[1].details?.map((detail) => detail.label), ['错误信息'])
  assert.match(rows[1].details?.[0].text ?? '', /搜索路径不存在/)
})

test('没有参数可作摘要时，失败行用错误文本兜底', () => {
  const rows = buildAgentTraceRows([
    action('execute_python'),
    observation('execute_python', { success: false, error: 'SyntaxError: invalid syntax' }),
  ])

  assert.equal(rows[1].summary, 'SyntaxError: invalid syntax')
  assert.equal(rows[1].details, undefined)
})

test('任务生命周期事件不进入轨迹列表', () => {
  const rows = buildAgentTraceRows([
    started(),
    plan(1),
    { type: 'agent_todo_started', todo_id: 't1', objective: '任务 1' },
    { type: 'agent_todo_completed', todo_id: 't1', result_summary: '完成' },
  ])

  assert.deepEqual(rows.map((row) => row.action), ['开始', '规划'])
})

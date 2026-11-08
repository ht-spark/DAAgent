/** 真实 token 用量的边界、累计与去重测试。 */
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { collectTokenUsage, formatTokenCount } from './tokenUsage.ts'

test('本轮独立展示，会话只累加助手任务且按运行 ID 去重', () => {
  const messages = [
    { role: 'user', usageSummary: { input_tokens: 999, output_tokens: 999 } },
    { role: 'assistant', runId: 'r1', usageSummary: { input_tokens: 100, output_tokens: 20 } },
    { role: 'assistant', runId: 'r1', usageSummary: { input_tokens: 100, output_tokens: 20 } },
    { role: 'assistant', runId: 'r2', usageSummary: { input_tokens: 40, output_tokens: 10 } },
  ]
  const usage = collectTokenUsage(messages, false)
  assert.equal(usage.current?.input, 40)
  assert.equal(usage.current?.total, 50)
  assert.equal(usage.session.input, 140)
  assert.equal(usage.session.output, 30)
  assert.equal(usage.session.total, 170)
  assert.equal(usage.session.runs, 2)
})

test('未知和旧记录不冒充零值，已知小计只能作为下界', () => {
  const usage = collectTokenUsage([
    { role: 'assistant', runId: 'old' },
    { role: 'assistant', runId: 'partial', usageSummary: {
      input_tokens: null, output_tokens: 5, known_input_tokens: 20,
    } },
  ], false)
  assert.equal(usage.session.input, null)
  assert.equal(usage.session.output, null)
  assert.equal(usage.session.total, null)
  assert.equal(formatTokenCount(usage.session.input, usage.session.knownInput), '≥20')
  assert.equal(formatTokenCount(usage.session.total, usage.session.knownTotal), '≥25')
})

test('新任务进行中不沿用上轮数值，会话保留已结束任务累计', () => {
  const usage = collectTokenUsage([
    { role: 'assistant', runId: 'r1', usageSummary: { input_tokens: 10, output_tokens: 2 } },
    { role: 'user' }, { role: 'assistant', runId: 'r2' },
  ], true)
  assert.equal(usage.current, null)
  assert.equal(usage.session.total, 12)
  assert.equal(usage.session.runs, 1)
})

test('空会话与真实零消耗不同，畸形计量保持未知', () => {
  assert.equal(collectTokenUsage([], false).current, null)
  assert.equal(collectTokenUsage([], false).session.runs, 0)
  const zero = collectTokenUsage([{ role: 'assistant', usageSummary: { input_tokens: 0, output_tokens: 0 } }], false)
  assert.equal(zero.current?.total, 0)
  assert.equal(formatTokenCount(zero.current!.total, 0), '0')
  for (const value of [null, [], {}, 'bad', { input_tokens: -1, output_tokens: '20' }, { input_tokens: Infinity, output_tokens: true }]) {
    const usage = collectTokenUsage([{ role: 'assistant', usageSummary: value }], false)
    assert.equal(usage.current?.total, null)
    assert.equal(formatTokenCount(usage.current!.total, usage.current!.knownTotal), '未知')
  }
})

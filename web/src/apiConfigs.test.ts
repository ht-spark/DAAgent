/** API 配置枚举测试。 */
import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  isReasoningEffort,
  REASONING_EFFORT_OPTIONS,
  reasoningEffortLabel,
} from './apiConfigs.ts'

test('思考程度仅提供约定的六个英文选项', () => {
  const expected = ['close', 'low', 'medium', 'high', 'xhigh', 'max']

  assert.deepEqual(REASONING_EFFORT_OPTIONS.map((option) => option.value), expected)
  assert.deepEqual(REASONING_EFFORT_OPTIONS.map((option) => option.label), expected)
  assert.equal(isReasoningEffort('off'), false)
  assert.equal(reasoningEffortLabel(undefined), 'close')
})

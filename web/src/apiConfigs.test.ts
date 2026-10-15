/** API 配置枚举测试。 */
import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  isReasoningEffort,
  REASONING_EFFORT_OPTIONS,
  reasoningEffortLabel,
} from './apiConfigs.ts'

test('思考程度：接口值保持英文，展示名使用中文', () => {
  const expectedValues = ['close', 'low', 'medium', 'high', 'xhigh', 'max']
  const expectedLabels = ['关闭', '低', '中', '高', '极高', '最大']

  assert.deepEqual(REASONING_EFFORT_OPTIONS.map((option) => option.value), expectedValues)
  assert.deepEqual(REASONING_EFFORT_OPTIONS.map((option) => option.label), expectedLabels)
  assert.equal(isReasoningEffort('off'), false)
  assert.equal(reasoningEffortLabel(undefined), '关闭')
  assert.equal(reasoningEffortLabel('xhigh'), '极高')
})

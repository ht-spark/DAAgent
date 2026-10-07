/** 常规设置枚举与本地缓存兼容性测试。 */
import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  DEFAULT_UI_SETTINGS,
  TERMINAL_TYPE_OPTIONS,
  isTerminalType,
  loadUiSettings,
} from './settings.ts'

test('终端类型只提供 PowerShell、Bash 和 Git Bash', () => {
  assert.deepEqual(
    TERMINAL_TYPE_OPTIONS.map((option) => option.value),
    ['powershell', 'bash', 'git-bash'],
  )
  assert.equal(isTerminalType('cmd'), false)
})

test('旧版常规设置缺少终端类型时默认使用 PowerShell', () => {
  const previous = globalThis.localStorage
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: () => JSON.stringify({ fontSize: 16 }),
      setItem: () => undefined,
    },
  })
  try {
    assert.deepEqual(loadUiSettings(), {
      fontSize: 16,
      terminalType: DEFAULT_UI_SETTINGS.terminalType,
    })
  } finally {
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      value: previous,
    })
  }
})

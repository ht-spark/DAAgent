/**
 * modalLayer 行为测试：Esc 只作用于最上层弹窗、页面滚动锁成对开关。
 *
 * 运行方式（Node 22+ 类型剥离）：
 *   node --experimental-strip-types --test src/modalLayer.test.ts
 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

type KeyListener = (event: { key: string }) => void

/** 极简 DOM 打桩：modalLayer 只用到 body 的 class / overflow 与 window 的按键监听 */
function installDom() {
  const classes = new Set<string>()
  const listeners = new Map<string, KeyListener>()
  const body = {
    style: { overflow: '' },
    classList: {
      toggle(name: string, force: boolean) {
        if (force) classes.add(name)
        else classes.delete(name)
      },
    },
  }
  const win = {
    addEventListener(type: string, listener: KeyListener) {
      listeners.set(type, listener)
    },
    removeEventListener(type: string) {
      listeners.delete(type)
    },
  }
  Object.assign(globalThis, {
    document: { body } as unknown as Document,
    window: win as unknown as Window & typeof globalThis,
  })
  return {
    classes,
    body,
    listenerCount: () => listeners.size,
    press(key: string) {
      const listener = listeners.get('keydown')
      if (!listener) throw new Error('没有注册 keydown 监听')
      listener({ key })
    },
  }
}

installDom()
const { pushModalLayer } = await import('./modalLayer.ts')

test('Esc 只回调最上层弹窗，收起后才轮到下层', () => {
  const dom = installDom()
  const calls: string[] = []
  const popLower = pushModalLayer(() => calls.push('下层'))
  const popUpper = pushModalLayer(() => calls.push('上层'))

  dom.press('Escape')
  assert.deepEqual(calls, ['上层'])

  popUpper()
  dom.press('Escape')
  assert.deepEqual(calls, ['上层', '下层'])

  popLower()
  assert.equal(dom.listenerCount(), 0, '全部关闭后应移除按键监听')
})

test('滚动锁与 body 标记成对开关', () => {
  const dom = installDom()
  assert.equal(dom.body.style.overflow, '')

  const pop = pushModalLayer(() => {})
  assert.equal(dom.body.style.overflow, 'hidden')
  assert.ok(dom.classes.has('is-modal-open'), '打开时应有 is-modal-open（氛围动画据此暂停）')

  pop()
  assert.equal(dom.body.style.overflow, '', '关闭后应恢复原有滚动状态')
  assert.equal(dom.classes.has('is-modal-open'), false)
  assert.equal(dom.listenerCount(), 0)
})

test('乱序注销不会提前释放滚动锁', () => {
  const dom = installDom()
  const calls: string[] = []
  const popLower = pushModalLayer(() => calls.push('下层'))
  const popUpper = pushModalLayer(() => calls.push('上层'))

  popLower()
  assert.equal(dom.body.style.overflow, 'hidden', '仍有弹窗打开时不应解锁')
  dom.press('Escape')
  assert.deepEqual(calls, ['上层'])

  popUpper()
  assert.equal(dom.body.style.overflow, '')
  assert.equal(dom.classes.has('is-modal-open'), false)
})

test('非 Esc 按键不触发回调', () => {
  const dom = installDom()
  let count = 0
  const pop = pushModalLayer(() => {
    count += 1
  })

  dom.press('Enter')
  dom.press('a')
  assert.equal(count, 0)

  pop()
})

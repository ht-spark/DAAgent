/**
 * modalLayer 行为测试：Esc 只作用于最上层弹窗、页面滚动锁成对开关、
 * Tab 焦点限制在最上层弹窗内部。
 *
 * 运行方式（Node 22+ 类型剥离）：
 *   node --experimental-strip-types --test src/modalLayer.test.ts
 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

type KeyListener = (event: KeyboardEvent) => void

type PressEvent = {
  key: string
  shiftKey: boolean
  prevented: boolean
  preventDefault: () => void
}

/** 极简 DOM 打桩：modalLayer 只用到 body 的 class / overflow、document.activeElement 与 window 的按键监听 */
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
  const doc: { body: unknown; activeElement: unknown } = { body, activeElement: null }
  const win = {
    addEventListener(type: string, listener: KeyListener) {
      listeners.set(type, listener)
    },
    removeEventListener(type: string) {
      listeners.delete(type)
    },
  }
  Object.assign(globalThis, {
    document: doc as unknown as Document,
    window: win as unknown as Window & typeof globalThis,
  })
  return {
    classes,
    body,
    doc,
    listenerCount: () => listeners.size,
    press(key: string, shiftKey = false): PressEvent {
      const listener = listeners.get('keydown')
      if (!listener) throw new Error('没有注册 keydown 监听')
      const event: PressEvent = {
        key,
        shiftKey,
        prevented: false,
        preventDefault() {
          event.prevented = true
        },
      }
      listener(event as unknown as KeyboardEvent)
      return event
    },
  }
}

type FakeFocusable = {
  focused: boolean
  getClientRects: () => unknown[]
  focus: () => void
}

/** 构造可控的「弹窗容器」桩：querySelectorAll 返回固定元素，focus 会同步 document.activeElement */
function makeContainer(count: number) {
  const els: FakeFocusable[] = []
  const container = {
    els,
    querySelectorAll: () => els,
    contains: (target: unknown) => els.includes(target as FakeFocusable),
    focus() {},
  }
  for (let i = 0; i < count; i += 1) {
    const el: FakeFocusable = {
      focused: false,
      // 可见性判定用 getClientRects().length，返回非空即视为可见
      getClientRects: () => [{}],
      focus() {
        els.forEach((item) => {
          item.focused = false
        })
        el.focused = true
        ;(globalThis as { document?: { activeElement?: unknown } }).document!.activeElement = el
      },
    }
    els.push(el)
  }
  return container
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

test('Tab 焦点在最上层弹窗内部首尾回绕', () => {
  const dom = installDom()
  // 两层弹窗同时打开：焦点陷阱只应作用于最上层
  const lower = makeContainer(2)
  const popLower = pushModalLayer(() => {}, lower as unknown as HTMLElement)
  const upper = makeContainer(3)
  const popUpper = pushModalLayer(() => {}, upper as unknown as HTMLElement)

  // 停在最后一个元素时按 Tab → 回绕到第一个，并拦截默认行为
  upper.els[2].focus()
  const forward = dom.press('Tab')
  assert.equal(forward.prevented, true, '到达末尾时应拦截默认 Tab')
  assert.equal(upper.els[0].focused, true, '应回绕到容器第一个元素')
  assert.equal(lower.els.some((el) => el.focused), false, '不应落到下层容器')

  // 停在第一个元素时按 Shift+Tab → 回绕到最后一个
  upper.els[0].focus()
  const backward = dom.press('Tab', true)
  assert.equal(backward.prevented, true)
  assert.equal(upper.els[2].focused, true)

  // 停在中间元素时按 Tab → 不拦截，交给浏览器按正常顺序移动
  upper.els[1].focus()
  const middle = dom.press('Tab')
  assert.equal(middle.prevented, false)

  popUpper()
  popLower()
})

test('焦点已在容器外时，Tab 会被拉回容器内', () => {
  const dom = installDom()
  const container = makeContainer(2)
  const pop = pushModalLayer(() => {}, container as unknown as HTMLElement)

  // 模拟焦点意外跑到弹窗外（例如浏览器地址栏或背景元素）
  dom.doc.activeElement = { outside: true }
  const event = dom.press('Tab')
  assert.equal(event.prevented, true)
  assert.equal(container.els[0].focused, true)

  pop()
})

test('未提供容器的弹窗不拦截 Tab', () => {
  const dom = installDom()
  const pop = pushModalLayer(() => {})

  const event = dom.press('Tab')
  assert.equal(event.prevented, false)

  pop()
})

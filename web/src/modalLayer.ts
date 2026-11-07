// 模态层统一管理：页面滚动锁、氛围动画暂停、Esc 只作用于最上层弹窗、Tab 焦点陷阱。
//
// 设置弹窗里可以再叠「编辑 API 配置」和「确认删除」两层弹窗。如果每层各自
// 监听 keydown 并各自改写 body.style.overflow，就会出现两个问题：
//   1. 一次 Esc 会同时关掉所有层（例如编辑弹窗和整个设置弹窗一起消失）；
//   2. 多层按不同顺序关闭时，滚动锁的保存值互相覆盖，页面残留无法滚动。
// 因此集中到本模块：谁最后注册谁在最上层，Esc 只回调它，滚动锁按栈深度成对开关。

type ModalLayer = {
  onEscape: () => void
  /** 该层的 DOM 容器；提供时 Tab 焦点被限制在容器内部循环 */
  container?: HTMLElement | null
  /** 注册时的外部焦点，最上层关闭后恢复到仍存在的触发控件。 */
  returnFocus: HTMLElement | null
}

const layers: ModalLayer[] = []
/** 打开弹窗前 body 的行内 overflow，为空表示当前没有锁 */
let savedBodyOverflow: string | null = null
let listening = false

/** 参与 Tab 循环的元素集合，沿用浏览器默认可聚焦的语义 */
const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

/** 在弹窗挂载完成后选择初始焦点；空弹窗由容器自身承接。 */
function focusLayer(container: HTMLElement) {
  const focusables = Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
    .filter((element) => element.getClientRects().length > 0)
  const first = focusables.find((element) => element.hasAttribute('data-modal-initial-focus')) ?? focusables[0]
  if (first) {
    first.focus()
  } else {
    container.tabIndex = -1
    container.focus()
  }
}

/**
 * 把 Tab 焦点限制在当前最上层弹窗内部。
 *
 * 不设陷阱时，用户按几次 Tab 就会走到遮罩背后的页面上：焦点跑到看不见的位置，
 * 屏幕阅读器也会跟着离开弹窗上下文。这里在首尾之间回绕，保持焦点始终在弹窗内。
 */
function trapTab(event: KeyboardEvent, container: HTMLElement) {
  const focusables = Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
    .filter((el) => el.getClientRects().length > 0)

  if (focusables.length === 0) {
    // 容器内暂无可聚焦元素时把焦点就地留住，避免漏到背景
    event.preventDefault()
    container.focus()
    return
  }

  const first = focusables[0]
  const last = focusables[focusables.length - 1]
  const active = document.activeElement as HTMLElement | null
  const inside = active !== null && container.contains(active)

  if (event.shiftKey) {
    if (!inside || active === first) {
      event.preventDefault()
      last.focus()
    }
    return
  }

  if (!inside || active === last) {
    event.preventDefault()
    first.focus()
  }
}

/** 键盘总入口：Esc 交给最上层弹窗，Tab 交给最上层弹窗的焦点陷阱 */
function handleKeyDown(event: KeyboardEvent) {
  if (layers.length === 0) return
  const layer = layers[layers.length - 1]

  if (event.key === 'Escape') {
    event.preventDefault()
    layer.onEscape()
    return
  }

  if (event.key === 'Tab' && layer.container) {
    trapTab(event, layer.container)
  }
}

function syncDom() {
  const hasLayer = layers.length > 0

  // 供 CSS 判断是否暂停氛围背景动画（遮罩下的持续重绘是界面卡顿的主要来源）
  document.body.classList.toggle('is-modal-open', hasLayer)

  if (hasLayer) {
    if (savedBodyOverflow === null) {
      savedBodyOverflow = document.body.style.overflow
      document.body.style.overflow = 'hidden'
    }
  } else if (savedBodyOverflow !== null) {
    document.body.style.overflow = savedBodyOverflow
    savedBodyOverflow = null
  }

  if (hasLayer && !listening) {
    window.addEventListener('keydown', handleKeyDown)
    listening = true
  } else if (!hasLayer && listening) {
    window.removeEventListener('keydown', handleKeyDown)
    listening = false
  }
}

/**
 * 当前是否有模态层处于打开状态。
 *
 * 供「非模态」浮层使用：这类浮层（例如聊天里的命令批准卡片）自己监听 Esc，
 * 但当上层还有真正的模态弹窗时，Esc 应当只作用于模态层，浮层不能抢走它。
 */
export function hasOpenModalLayer(): boolean {
  return layers.length > 0
}

/**
 * 注册一个模态层。
 *
 * @param onEscape 该层处于最上层时按下 Esc 的回调
 * @param container 该层的 DOM 容器；传入后 Tab 焦点会被限制在容器内部
 * @returns 注销函数，组件卸载或弹窗关闭时调用
 */
export function pushModalLayer(
  onEscape: () => void,
  container?: HTMLElement | null,
): () => void {
  const active = document.activeElement as HTMLElement | null
  const layer: ModalLayer = {
    onEscape,
    container,
    returnFocus: active && !container?.contains(active) ? active : null,
  }
  layers.push(layer)
  syncDom()
  // 等同一轮 React 挂载和 inert 更新完成，避免聚焦到尚未就绪的容器。
  queueMicrotask(() => {
    if (container && container.isConnected !== false && layers[layers.length - 1] === layer) {
      focusLayer(container)
    }
  })

  return () => {
    const index = layers.indexOf(layer)
    if (index === -1) return
    const wasTop = index === layers.length - 1
    layers.splice(index, 1)
    syncDom()
    if (!wasTop) return
    const parent = layers[layers.length - 1]?.container
    const target = layer.returnFocus
    if (target && target.isConnected !== false && typeof target.focus === 'function'
      && (!parent || parent.contains(target))) {
      target.focus()
    } else if (parent) {
      focusLayer(parent)
    }
  }
}

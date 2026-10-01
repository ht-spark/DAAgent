// 模态层统一管理：页面滚动锁、氛围动画暂停、Esc 只作用于最上层弹窗。
//
// 设置弹窗里可以再叠「编辑 API 配置」和「确认删除」两层弹窗。如果每层各自
// 监听 keydown 并各自改写 body.style.overflow，就会出现两个问题：
//   1. 一次 Esc 会同时关掉所有层（例如编辑弹窗和整个设置弹窗一起消失）；
//   2. 多层按不同顺序关闭时，滚动锁的保存值互相覆盖，页面残留无法滚动。
// 因此集中到本模块：谁最后注册谁在最上层，Esc 只回调它，滚动锁按栈深度成对开关。

type ModalLayer = {
  onEscape: () => void
}

const layers: ModalLayer[] = []
/** 打开弹窗前 body 的行内 overflow，为空表示当前没有锁 */
let savedBodyOverflow: string | null = null
let listening = false

/** 只把 Esc 交给最上层弹窗，避免一次按键消费多层 */
function handleKeyDown(event: KeyboardEvent) {
  if (event.key !== 'Escape' || layers.length === 0) return
  layers[layers.length - 1].onEscape()
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
 * 注册一个模态层。
 *
 * @param onEscape 该层处于最上层时按下 Esc 的回调
 * @returns 注销函数，组件卸载或弹窗关闭时调用
 */
export function pushModalLayer(onEscape: () => void): () => void {
  const layer: ModalLayer = { onEscape }
  layers.push(layer)
  syncDom()

  return () => {
    const index = layers.indexOf(layer)
    if (index === -1) return
    layers.splice(index, 1)
    syncDom()
  }
}

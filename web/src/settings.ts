// 通用界面设置（localStorage），当前仅字体大小。

export const FONT_SIZE_OPTIONS = [
  { value: 12, label: '小', scale: 12 / 14 },
  { value: 14, label: '标准', scale: 1 },
  { value: 16, label: '大', scale: 16 / 14 },
  { value: 18, label: '特大', scale: 18 / 14 },
] as const

export type UiSettings = {
  /** 界面正文字号（px） */
  fontSize: number
}

const KEY_UI = 'mmagent.uiSettings'

export const DEFAULT_UI_SETTINGS: UiSettings = {
  fontSize: 14,
}

export function loadUiSettings(): UiSettings {
  try {
    const raw = localStorage.getItem(KEY_UI)
    if (!raw) return { ...DEFAULT_UI_SETTINGS }
    const parsed = JSON.parse(raw) as Partial<UiSettings>
    const fontSize = Number(parsed.fontSize)
    const known = FONT_SIZE_OPTIONS.some((o) => o.value === fontSize)
    return {
      fontSize: known ? fontSize : DEFAULT_UI_SETTINGS.fontSize,
    }
  } catch {
    return { ...DEFAULT_UI_SETTINGS }
  }
}

export function saveUiSettings(settings: UiSettings) {
  localStorage.setItem(KEY_UI, JSON.stringify(settings))
}

/** 把字体偏好写入 CSS 变量，供样式表按比例缩放可读文本。 */
export function applyUiSettings(settings: UiSettings) {
  const option = FONT_SIZE_OPTIONS.find((o) => o.value === settings.fontSize) ?? FONT_SIZE_OPTIONS[1]
  const root = document.documentElement
  root.style.setProperty('--app-font-size', `${settings.fontSize}px`)
  root.style.setProperty('--app-font-scale', String(option.scale))
}

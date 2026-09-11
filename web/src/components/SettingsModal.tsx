import { useEffect, useMemo, useState } from 'react'

import {
  FONT_SIZE_OPTIONS,
  applyUiSettings,
  loadUiSettings,
  saveUiSettings,
  type UiSettings,
} from '../settings'

type Props = {
  open: boolean
  onClose: () => void
}

const SEARCH_PLACEHOLDER = '搜索设置'

export default function SettingsModal({ open, onClose }: Props) {
  const [settings, setSettings] = useState<UiSettings>(() => loadUiSettings())
  const [query, setQuery] = useState('')

  useEffect(() => {
    if (!open) return
    setSettings(loadUiSettings())
    setQuery('')
  }, [open])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  const showFontSize = useMemo(() => {
    if (!query.trim()) return true
    const q = query.trim().toLowerCase()
    return '字体大小 界面字号 font size 常规 通用'.toLowerCase().includes(q) || q.includes('字')
  }, [query])

  function updateFontSize(size: number) {
    const next = { ...settings, fontSize: size }
    setSettings(next)
    saveUiSettings(next)
    applyUiSettings(next)
  }

  if (!open) return null

  return (
    <div className="settings-mask" onClick={onClose} role="presentation">
      <div
        className="settings-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="settings-sidebar">
          <div className="settings-sidebar-head">
            <h2 id="settings-title">设置</h2>
            <button type="button" className="settings-close" onClick={onClose} aria-label="关闭设置">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                <path d="M6 6l12 12M18 6L6 18" strokeLinecap="round" />
              </svg>
            </button>
          </div>
          <input
            className="settings-search"
            type="search"
            placeholder={SEARCH_PLACEHOLDER}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <nav className="settings-nav" aria-label="设置分类">
            <div className="settings-nav-group">个人</div>
            <button type="button" className="settings-nav-item active">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
                <path d="M4 7h10M18 7h2M4 17h2M10 17h10" strokeLinecap="round" />
                <circle cx="16" cy="7" r="2" />
                <circle cx="8" cy="17" r="2" />
              </svg>
              <span>常规</span>
            </button>
          </nav>
        </div>

        <div className="settings-main">
          <header className="settings-main-head">
            <h1>常规</h1>
          </header>

          <section className="settings-section">
            <h3 className="settings-section-title">常规</h3>
            <div className="settings-card">
              {showFontSize && (
                <div className="settings-row">
                  <div className="settings-row-copy">
                    <div className="settings-row-label">界面字体大小</div>
                    <p className="settings-row-desc">调整聊天、报告等正文的显示字号，立即生效并保存到本地。</p>
                  </div>
                  <div className="settings-row-control">
                    <select
                      className="settings-select"
                      value={settings.fontSize}
                      onChange={(e) => updateFontSize(Number(e.target.value))}
                      aria-label="界面字体大小"
                    >
                      {FONT_SIZE_OPTIONS.map((opt) => (
                        <option key={opt.value} value={opt.value}>
                          {opt.label}（{opt.value}px）
                        </option>
                      ))}
                    </select>
                  </div>
                </div>
              )}
              {!showFontSize && (
                <p className="settings-empty">没有匹配「{query.trim()}」的设置项</p>
              )}
            </div>
          </section>
        </div>
      </div>
    </div>
  )
}

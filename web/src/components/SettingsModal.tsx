import { useEffect, useMemo, useRef, useState } from 'react'

import ApiManager from '../pages/ApiManager'
import { persistGeneralSettings } from '../api'
import { pushModalLayer } from '../modalLayer'
import {
  FONT_SIZE_OPTIONS,
  TERMINAL_TYPE_OPTIONS,
  applyUiSettings,
  loadUiSettings,
  saveUiSettings,
  type UiSettings,
} from '../settings'

type Props = {
  open: boolean
  onClose: () => void
}

type SettingsTab = 'general' | 'api'

const SEARCH_PLACEHOLDER = '搜索设置'

export default function SettingsModal({ open, onClose }: Props) {
  const [settings, setSettings] = useState<UiSettings>(() => loadUiSettings())
  const [query, setQuery] = useState('')
  const [tab, setTab] = useState<SettingsTab>('general')
  // API 分类首次进入后才挂载，之后保持挂载：来回切分类不会重新请求后端配置，
  // 也不会先闪一次空列表（原来的写法每次切换都会卸载重挂 ApiManager）。
  const [apiMounted, setApiMounted] = useState(false)

  // onClose 通常是父组件里的内联箭头函数，每次渲染都是新引用；
  // 用 ref 取最新值，避免监听器每次渲染都重新注册。
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  // 弹窗容器：把 Tab 焦点限制在弹窗内部
  const dialogRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!open) return
    setSettings(loadUiSettings())
    setQuery('')
    setTab('general')
    setApiMounted(false)
    // 压入模态层：统一接管 Esc 与页面滚动锁，并让氛围背景动画停下来
    return pushModalLayer(() => onCloseRef.current(), dialogRef.current)
  }, [open])

  const q = query.trim().toLowerCase()
  const showFontSize = useMemo(() => {
    if (!q) return true
    return '字体大小 界面字号 font size 常规 通用'.toLowerCase().includes(q) || q.includes('字')
  }, [q])
  const showTerminalType = useMemo(() => {
    if (!q) return true
    return '终端 shell powershell bash git bash 命令行 常规 通用'.includes(q)
  }, [q])
  const showApiTab = useMemo(() => {
    if (!q) return true
    return 'api 模型 服务 密钥 key 检索 mineru tavily 配置'.includes(q) || q.includes('a')
  }, [q])
  const showGeneralTab = useMemo(() => {
    if (!q) return true
    return showFontSize || showTerminalType
  }, [q, showFontSize, showTerminalType])

  function updateFontSize(size: number) {
    const next = { ...settings, fontSize: size }
    setSettings(next)
    saveUiSettings(next)
    applyUiSettings(next)
  }

  function updateTerminalType(terminalType: UiSettings['terminalType']) {
    const next = { ...settings, terminalType }
    setSettings(next)
    saveUiSettings(next)
    void persistGeneralSettings(terminalType)
  }

  function selectTab(next: SettingsTab) {
    setTab(next)
    if (next === 'api') setApiMounted(true)
  }

  if (!open) return null

  return (
    <div className="settings-mask" onClick={onClose} role="presentation">
      <div
        ref={dialogRef}
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
            aria-label="搜索设置"
            placeholder={SEARCH_PLACEHOLDER}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <nav className="settings-nav" aria-label="设置分类">
            <div className="settings-nav-group">个人</div>
            {showGeneralTab && (
              <button
                type="button"
                className={`settings-nav-item ${tab === 'general' ? 'active' : ''}`}
                aria-current={tab === 'general' ? 'page' : undefined}
                onClick={() => selectTab('general')}
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
                  <path d="M4 7h10M18 7h2M4 17h2M10 17h10" strokeLinecap="round" />
                  <circle cx="16" cy="7" r="2" />
                  <circle cx="8" cy="17" r="2" />
                </svg>
                <span>常规</span>
              </button>
            )}
            <div className="settings-nav-group">服务</div>
            {showApiTab && (
              <button
                type="button"
                className={`settings-nav-item ${tab === 'api' ? 'active' : ''}`}
                aria-current={tab === 'api' ? 'page' : undefined}
                onClick={() => selectTab('api')}
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
                  <circle cx="12" cy="12" r="9" />
                  <path d="M14.5 9.5 10 12.5 14 15.5" strokeLinecap="round" strokeLinejoin="round" />
                  <path d="M12 3v3M12 18v3M3 12h3M18 12h3" strokeLinecap="round" />
                </svg>
                <span>API 配置</span>
              </button>
            )}
          </nav>
        </div>

        <div className={`settings-main${tab === 'api' ? ' settings-main--api' : ''}`}>
          {tab === 'general' && (
            <>
              <header className="settings-main-head">
                <h1>常规</h1>
              </header>

              <section className="settings-section">
                <h3 className="settings-section-title">界面与运行偏好</h3>
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
                  {showTerminalType && (
                    <div className="settings-row">
                      <div className="settings-row-copy">
                        <div className="settings-row-label">默认终端类型</div>
                        <p className="settings-row-desc">Agent 的终端命令固定使用此 Shell；修改后对新任务生效。</p>
                      </div>
                      <div className="settings-row-control">
                        <select
                          className="settings-select"
                          value={settings.terminalType}
                          onChange={(event) => updateTerminalType(event.target.value as UiSettings['terminalType'])}
                          aria-label="默认终端类型"
                        >
                          {TERMINAL_TYPE_OPTIONS.map((option) => (
                            <option key={option.value} value={option.value}>{option.label}</option>
                          ))}
                        </select>
                      </div>
                    </div>
                  )}
                  {!showFontSize && !showTerminalType && (
                    <div className="settings-row">
                      <div className="settings-row-copy">
                        <div className="settings-row-label">没有匹配的设置</div>
                        <p className="settings-row-desc">试试搜索「字体」「终端」或「API」。</p>
                      </div>
                    </div>
                  )}
                </div>
              </section>
            </>
          )}

          {apiMounted && (
            <div className={`settings-api-pane${tab === 'api' ? '' : ' is-hidden'}`}>
              <header className="settings-main-head">
                <h1>API 配置</h1>
                <p className="settings-main-lead">管理模型与扩展服务；配置保存在本机。</p>
              </header>
              <div className="settings-api-host">
                <ApiManager embedded active={tab === 'api'} />
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

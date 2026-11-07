import { isValidElement, useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown'
import rehypeKatex from 'rehype-katex'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'

import { pushModalLayer } from '../modalLayer'

import 'katex/dist/katex.min.css'

let mermaidInitialized = false

function normalizeMathDelimiters(content: string): string {
  return content
    .replace(/\\\[([\s\S]*?)\\\]/g, (_match, math: string) => `\n$$\n${math.trim()}\n$$\n`)
    .replace(/\\\(([\s\S]*?)\\\)/g, (_match, math: string) => `$${math}$`)
}

/** 从 Markdown 代码块中识别 Mermaid 源码，普通代码块保持原样。 */
function getMermaidCode(children: ReactNode): string | null {
  if (!isValidElement<{ className?: string; children?: ReactNode }>(children)) return null
  const code = String(children.props.children ?? '').replace(/\n$/, '')
  const hasMermaidLanguage = /(^|\s)language-mermaid(?:\s|$)/i.test(
    children.props.className ?? '',
  )
  const hasMermaidSyntax = /^\s*(?:%%\{[\s\S]*?\}%%\s*)?(?:flowchart|graph|sequenceDiagram|classDiagram|stateDiagram(?:-v2)?|erDiagram|journey|gantt|pie|mindmap|timeline|gitGraph|quadrantChart|xychart-beta|sankey-beta|block-beta|architecture-beta)\b/.test(code)
  return hasMermaidLanguage || hasMermaidSyntax ? code : null
}

/** 将 Mermaid 代码块渲染为隔离的 SVG 图表。 */
function MermaidDiagram({ code }: { code: string }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const diagramId = useId().replace(/[^a-zA-Z0-9_-]/g, '')
  const [error, setError] = useState<string | null>(null)
  const [rendered, setRendered] = useState(false)
  const [svg, setSvg] = useState('')
  const [fullscreen, setFullscreen] = useState(false)

  function openFullscreen() {
    setFullscreen(true)
    void document.documentElement.requestFullscreen?.().catch(() => undefined)
  }

  function closeFullscreen() {
    setFullscreen(false)
    if (document.fullscreenElement) void document.exitFullscreen()
  }

  useEffect(() => {
    let cancelled = false
    setError(null)
    setRendered(false)
    setSvg('')
    setFullscreen(false)
    containerRef.current?.replaceChildren()

    void import('mermaid')
      .then(async ({ default: mermaid }) => {
        if (!mermaidInitialized) {
          mermaid.initialize({
            startOnLoad: false,
            securityLevel: 'strict',
            theme: 'base',
            themeVariables: {
              primaryColor: '#f3f2ef',
              primaryTextColor: '#2b2a28',
              primaryBorderColor: '#6f6c67',
              lineColor: '#6f6e69',
              secondaryColor: '#f6f5f2',
              tertiaryColor: '#fdfcfa',
              fontFamily: 'Microsoft YaHei, PingFang SC, Inter, sans-serif',
              fontSize: '16px',
            },
            flowchart: {
              htmlLabels: false,
              nodeSpacing: 52,
              rankSpacing: 64,
              padding: 18,
              useMaxWidth: false,
            },
          })
          mermaidInitialized = true
        }
        const { svg, bindFunctions } = await mermaid.render(`mermaid-${diagramId}`, code)
        if (cancelled || !containerRef.current) return
        containerRef.current.innerHTML = svg
        bindFunctions?.(containerRef.current)
        setSvg(svg)
        setRendered(true)
      })
      .catch((reason: unknown) => {
        if (cancelled) return
        setError(reason instanceof Error ? reason.message : '图表语法无效')
      })

    return () => { cancelled = true }
  }, [code, diagramId])

  if (error) {
    return (
      <div className="mermaid-diagram mermaid-diagram-error" role="status">
        <p>流程图无法渲染，已保留原始 Mermaid 代码。</p>
        <pre><code>{code}</code></pre>
      </div>
    )
  }

  return (
    <>
      <div className="mermaid-diagram" aria-label="Mermaid 流程图">
        {rendered && (
          <button
            type="button"
            className="mermaid-diagram-expand"
            onClick={openFullscreen}
            aria-label="全屏查看流程图"
            title="全屏查看"
          >
            <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="M8.5 4H4v4.5M15.5 4H20v4.5M20 15.5V20h-4.5M4 15.5V20h4.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
        )}
        {!rendered && <p className="mermaid-diagram-loading">正在渲染流程图…</p>}
        <div ref={containerRef} className={rendered ? '' : 'is-rendering'} />
      </div>
      {fullscreen && svg && (
        <MermaidFullscreen svg={svg} onClose={closeFullscreen} />
      )}
    </>
  )
}

/** 在独立模态层中查看完整流程图，避免正文区域限制图表尺寸。 */
function MermaidFullscreen({ svg, onClose }: { svg: string; onClose: () => void }) {
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  // 弹窗容器：把 Tab 焦点限制在弹窗内部
  const dialogRef = useRef<HTMLElement | null>(null)

  useEffect(() => pushModalLayer(() => onCloseRef.current(), dialogRef.current), [])

  useEffect(() => {
    function handleFullscreenChange() {
      if (!document.fullscreenElement) onCloseRef.current()
    }
    document.addEventListener('fullscreenchange', handleFullscreenChange)
    return () => document.removeEventListener('fullscreenchange', handleFullscreenChange)
  }, [])

  return createPortal(
    <div className="mermaid-fullscreen-mask" onClick={onClose} role="presentation">
      <section
        ref={dialogRef}
        className="mermaid-fullscreen-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="流程图全屏预览"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="mermaid-fullscreen-head">
          <div>
            <strong>流程图预览</strong>
            <span>按 Esc 退出全屏</span>
          </div>
          <button type="button" onClick={onClose} aria-label="关闭全屏预览" data-modal-initial-focus>
            <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="M6 6l12 12M18 6 6 18" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
            </svg>
          </button>
        </header>
        <div
          className="mermaid-fullscreen-canvas"
          dangerouslySetInnerHTML={{ __html: svg }}
        />
      </section>
    </div>,
    document.body,
  )
}

/** 可选：把 Markdown 里的相对资源地址改写为可访问 URL（预览产物图片用） */
export default function MarkdownContent({
  content,
  transformUrl,
}: {
  content: string
  transformUrl?: (url: string) => string
}) {
  return (
    <div className="markdown-content">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeKatex]}
        urlTransform={transformUrl ? (url) => defaultUrlTransform(transformUrl(url)) : undefined}
        components={{
          pre({ children, ...props }) {
            const mermaidCode = getMermaidCode(children)
            return mermaidCode === null
              ? <pre {...props}>{children}</pre>
              : <MermaidDiagram code={mermaidCode} />
          },
        }}
      >
        {normalizeMathDelimiters(content)}
      </ReactMarkdown>
    </div>
  )
}

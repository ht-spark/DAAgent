import { useCallback, useEffect, useRef, useState } from 'react'

import {
  convertDocumentsToMarkdown,
  deleteConversionRecord,
  getConvertibleFormats,
  listConversionHistory,
  readConversionContent,
  saveConvertedMarkdown,
  selectSystemFolder,
  type ConvertibleFormats,
  type DocumentConversionItem,
} from '../api'
import MarkdownContent from '../components/MarkdownContent'

/** 预览区渲染上限：超长 Markdown 全量渲染会明显卡顿，这里截断并明确提示 */
const PREVIEW_CHAR_LIMIT = 20000

/** 记住上次的保存目录，避免每次都要重新选 */
const SAVE_DIR_STORAGE_KEY = 'daagent.converter.saveDir'

/** 后端未就绪时的兜底格式列表，保证提示文案始终可用 */
const FALLBACK_EXTENSIONS = [
  '.pdf',
  '.docx',
  '.pptx',
  '.xlsx',
  '.xls',
  '.html',
  '.htm',
  '.epub',
  '.ipynb',
  '.csv',
  '.txt',
  '.md',
  '.json',
  '.xml',
]

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function markdownFileName(filename: string): string {
  return `${filename.replace(/\.[^.]+$/, '') || 'document'}.md`
}

/** 转换时间：只显示时分秒，表格里更清爽 */
function formatClock(iso: string): string {
  const time = new Date(iso)
  if (Number.isNaN(time.getTime())) return '—'
  return time.toLocaleTimeString('zh-CN', { hour12: false })
}

function readRememberedSaveDir(): string {
  try {
    return window.localStorage.getItem(SAVE_DIR_STORAGE_KEY) ?? ''
  } catch {
    return ''
  }
}

export default function DocConverter() {
  const [formats, setFormats] = useState<ConvertibleFormats>({
    extensions: FALLBACK_EXTENSIONS,
    max_bytes: 0,
    default_save_dir: '',
  })
  // 全部转换记录：启动时从后端历史载入，未删除的会一直保留
  const [records, setRecords] = useState<DocumentConversionItem[]>([])
  const [converting, setConverting] = useState(false)
  const [dragOver, setDragOver] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [previewRecord, setPreviewRecord] = useState<DocumentConversionItem | null>(null)
  const [previewContent, setPreviewContent] = useState<string | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewError, setPreviewError] = useState('')
  const [saveRecord, setSaveRecord] = useState<DocumentConversionItem | null>(null)
  const [saveName, setSaveName] = useState('')
  const [saveDir, setSaveDir] = useState('')
  const [saving, setSaving] = useState(false)
  const [pickingDir, setPickingDir] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)
  const lastRowRef = useRef<HTMLTableRowElement>(null)
  /** 正文按需从后端读取，读过的缓存住避免重复请求 */
  const contentCache = useRef(new Map<number, string>())
  /** 新转换完成时把最新一行滚进视野 */
  const pendingScroll = useRef(false)

  // 支持格式与默认保存目录以服务端为准，避免前后端各维护一份
  useEffect(() => {
    void getConvertibleFormats()
      .then(setFormats)
      .catch(() => {
        /* 后端未就绪时沿用兜底列表，转换时再报错 */
      })
  }, [])

  // 打开页面就把历史记录列出来（未删除的一直保留）
  useEffect(() => {
    void listConversionHistory()
      .then(setRecords)
      .catch(() => {
        /* 后端未就绪时先空着，转换时再提示 */
      })
  }, [])

  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(''), 4000)
    return () => window.clearTimeout(timer)
  }, [notice])

  // 新增记录后把该行滚进视野
  useEffect(() => {
    if (!pendingScroll.current) return
    pendingScroll.current = false
    lastRowRef.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [records])

  // 弹窗按 Esc 关闭
  useEffect(() => {
    if (!previewRecord && !saveRecord) return
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== 'Escape') return
      if (saving || pickingDir) return
      if (saveRecord) setSaveRecord(null)
      else setPreviewRecord(null)
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [previewRecord, saveRecord, saving, pickingDir])

  const extensions = formats.extensions.length > 0 ? formats.extensions : FALLBACK_EXTENSIONS

  const convertFiles = useCallback(
    async (fileList: FileList | null | undefined) => {
      if (!fileList || fileList.length === 0) return
      setError('')
      setNotice('')

      // 前端先按白名单与体积上限过滤，避免无谓的上传与等待
      const accepted: File[] = []
      const rejected: string[] = []
      for (const file of Array.from(fileList)) {
        const dot = file.name.lastIndexOf('.')
        const suffix = dot >= 0 ? file.name.slice(dot).toLowerCase() : ''
        if (!extensions.includes(suffix)) {
          rejected.push(`${file.name}（不支持 ${suffix || '无扩展名'}）`)
          continue
        }
        if (formats.max_bytes > 0 && file.size > formats.max_bytes) {
          rejected.push(`${file.name}（超过 ${formatBytes(formats.max_bytes)}）`)
          continue
        }
        accepted.push(file)
      }

      if (rejected.length > 0) {
        setError(`已跳过 ${rejected.length} 个文件：${rejected.join('、')}`)
      }
      if (accepted.length === 0) {
        if (fileInput.current) fileInput.current.value = ''
        return
      }

      setConverting(true)
      try {
        const items = await convertDocumentsToMarkdown(accepted)
        if (items.length === 0) {
          setError('后端未返回转换结果，请检查服务日志后重试。')
          return
        }
        pendingScroll.current = true
        setRecords((current) => [...current, ...items])
        const failed = items.filter((item) => !item.ok).length
        setNotice(
          failed > 0
            ? `完成 ${items.length} 个文件，其中 ${failed} 个转换失败`
            : `已转换 ${items.length} 个文件`,
        )
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : '文档转换失败。')
      } finally {
        setConverting(false)
        if (fileInput.current) fileInput.current.value = ''
      }
    },
    [extensions, formats.max_bytes],
  )

  /** 按需读取正文（成功记录才有），带会话内缓存 */
  const loadMarkdown = useCallback(async (record: DocumentConversionItem) => {
    const cached = contentCache.current.get(record.id)
    if (cached !== undefined) return cached
    const text = await readConversionContent(record.id)
    contentCache.current.set(record.id, text)
    return text
  }, [])

  function handleDrop(event: React.DragEvent) {
    event.preventDefault()
    setDragOver(false)
    if (converting) return
    void convertFiles(event.dataTransfer.files)
  }

  async function openPreview(record: DocumentConversionItem) {
    setPreviewRecord(record)
    setPreviewContent(null)
    setPreviewError('')
    if (!record.ok) return
    setPreviewLoading(true)
    try {
      setPreviewContent(await loadMarkdown(record))
    } catch (reason) {
      setPreviewError(reason instanceof Error ? reason.message : '读取转换内容失败。')
    } finally {
      setPreviewLoading(false)
    }
  }

  async function copyRecord(record: DocumentConversionItem) {
    try {
      const text = await loadMarkdown(record)
      await navigator.clipboard.writeText(text)
      setNotice(`已复制「${record.filename}」的 Markdown 全文`)
    } catch {
      setError('复制失败：浏览器拒绝了剪贴板访问，请改用「下载」')
    }
  }

  function openSaveDialog(record: DocumentConversionItem) {
    setPreviewRecord(null)
    setSaveRecord(record)
    setSaveName(markdownFileName(record.filename))
    setSaveDir(readRememberedSaveDir() || formats.default_save_dir || '')
    setError('')
  }

  async function pickDirectory() {
    if (pickingDir) return
    setPickingDir(true)
    setError('')
    try {
      const picked = await selectSystemFolder()
      if (picked) setSaveDir(picked)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法打开系统文件夹选择框。')
    } finally {
      setPickingDir(false)
    }
  }

  async function confirmSave() {
    if (!saveRecord || saving) return
    const directory = saveDir.trim()
    if (!directory) {
      setError('请先填写或选择保存目录。')
      return
    }
    setSaving(true)
    setError('')
    try {
      const saved = await saveConvertedMarkdown({
        record_id: saveRecord.id,
        filename: saveName.trim() || markdownFileName(saveRecord.filename),
        directory,
      })
      try {
        window.localStorage.setItem(SAVE_DIR_STORAGE_KEY, directory)
      } catch {
        /* 浏览器禁用存储时忽略，不影响保存 */
      }
      setSaveRecord(null)
      setNotice(`已保存到 ${saved.saved_path}`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存失败。')
    } finally {
      setSaving(false)
    }
  }

  async function removeRecord(record: DocumentConversionItem) {
    setError('')
    try {
      await deleteConversionRecord(record.id)
      contentCache.current.delete(record.id)
      setRecords((current) => current.filter((item) => item.id !== record.id))
      if (previewRecord?.id === record.id) setPreviewRecord(null)
      if (saveRecord?.id === record.id) setSaveRecord(null)
      setNotice(`已删除「${record.filename}」的转换记录`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '删除转换记录失败。')
    }
  }

  const okCount = records.filter((item) => item.ok).length
  const failedCount = records.length - okCount
  const totalChars = records.reduce((sum, item) => sum + item.char_count, 0)

  return (
    <div className="page conv-page">
      <header className="conv-head">
        <div>
          <h1 className="conv-title">文档转换</h1>
          <p className="conv-sub">
            把 PDF、Word、PPT、Excel、网页等文档排成 Markdown 文本，可预览、另存到本机或复制全文。
          </p>
        </div>
        <div className="conv-seal" aria-hidden="true">稿</div>
      </header>

      {error && <div className="err-box">{error}</div>}
      {notice && <div className="conv-notice">{notice}</div>}

      <section
        className={`conv-tray ${dragOver ? 'is-drag' : ''} ${converting ? 'is-busy' : ''} ${records.length > 0 ? 'is-compact' : ''}`}
        onDragOver={(event) => {
          event.preventDefault()
          setDragOver(true)
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={handleDrop}
        onClick={() => !converting && fileInput.current?.click()}
        role="button"
        tabIndex={0}
        aria-label="选择或拖入待转换文档"
        onKeyDown={(event) => {
          if (converting) return
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault()
            fileInput.current?.click()
          }
        }}
      >
        <input
          ref={fileInput}
          type="file"
          multiple
          hidden
          accept={extensions.join(',')}
          onChange={(event) => void convertFiles(event.target.files)}
        />
        {converting ? (
          <div className="conv-composing">
            <div className="conv-composing-lines" aria-hidden="true">
              <span />
              <span />
              <span />
            </div>
            <p className="conv-tray-title">正在排字…</p>
            <p className="conv-tray-hint">
              长篇 PDF 首次解析需要加载识别模型，可能耗时十几秒，请保持页面打开。
            </p>
          </div>
        ) : (
          <>
            <span className="conv-tray-icon" aria-hidden="true">
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M12 16V4" />
                <path d="m7 9 5-5 5 5" />
                <path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3" />
              </svg>
            </span>
            <p className="conv-tray-title">
              {records.length > 0 ? '拖拽或点击继续添加文档' : '点击或拖拽文件到此处转换'}
            </p>
            <p className="conv-tray-hint">
              {records.length > 0
                ? '支持 PDF / Word / PPT / Excel 等常见文档'
                : `支持以下格式${formats.max_bytes > 0 ? `，单个文件不超过 ${formatBytes(formats.max_bytes)}` : ''}`}
            </p>
            {records.length === 0 && (
              <ul className="conv-chips">
                {extensions.map((ext) => (
                  <li key={ext}>{ext}</li>
                ))}
              </ul>
            )}
          </>
        )}
      </section>

      {records.length > 0 && (
        <>
          <dl className="conv-stats">
            <div>
              <dt>已转换</dt>
              <dd>{records.length}</dd>
            </div>
            <div>
              <dt>成功</dt>
              <dd className="is-ok">{okCount}</dd>
            </div>
            <div>
              <dt>失败</dt>
              <dd className={failedCount > 0 ? 'is-bad' : undefined}>{failedCount}</dd>
            </div>
            <div>
              <dt>总字符</dt>
              <dd>{totalChars.toLocaleString()}</dd>
            </div>
          </dl>

          <div className="conv-table-wrap">
            <table className="conv-table">
              <thead>
                <tr>
                  <th className="conv-col-name">文件名称</th>
                  <th className="conv-col-time">转换时间</th>
                  <th className="conv-col-status">状态</th>
                  <th className="conv-col-actions">操作</th>
                </tr>
              </thead>
              <tbody>
                {records.map((item, index) => (
                  <tr
                    key={item.id}
                    ref={index === records.length - 1 ? lastRowRef : undefined}
                    className={item.ok ? undefined : 'is-failed'}
                  >
                    <td className="conv-col-name">
                      <span className="conv-name">{item.filename}</span>
                      <span className="conv-name-meta">
                        {item.ok
                          ? `${item.char_count.toLocaleString()} 字符 · ${formatBytes(item.size_bytes)}`
                          : '未生成内容'}
                      </span>
                    </td>
                    <td className="conv-col-time">
                      <span className="conv-clock">{formatClock(item.converted_at)}</span>
                      <span className="conv-clock-meta">
                        {item.ok ? `耗时 ${(item.elapsed_ms / 1000).toFixed(1)}s` : '—'}
                      </span>
                    </td>
                    <td className="conv-col-status">
                      <span className={`conv-status ${item.ok ? 'is-ok' : 'is-fail'}`}>
                        {item.ok ? '成功' : '失败'}
                      </span>
                    </td>
                    <td className="conv-col-actions">
                      <button type="button" className="api-btn" onClick={() => void openPreview(item)}>
                        预览
                      </button>
                      <button
                        type="button"
                        className="api-btn"
                        disabled={!item.ok}
                        onClick={() => openSaveDialog(item)}
                      >
                        下载
                      </button>
                      <button
                        type="button"
                        className="api-btn danger"
                        onClick={() => void removeRecord(item)}
                      >
                        删除
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {previewRecord && (
        <div
          className="api-modal-mask"
          role="presentation"
          onClick={() => setPreviewRecord(null)}
        >
          <div
            className="api-modal conv-modal-preview"
            role="dialog"
            aria-modal="true"
            aria-labelledby="conv-preview-title"
            onClick={(event) => event.stopPropagation()}
          >
            <header className="api-modal-head">
              <div>
                <h2 id="conv-preview-title">{previewRecord.filename}</h2>
                <p className="api-modal-lead">
                  {previewRecord.ok
                    ? `Markdown · ${previewRecord.char_count.toLocaleString()} 字符 · 转换时间 ${formatClock(previewRecord.converted_at)}`
                    : '转换失败，未生成内容'}
                </p>
              </div>
              <button
                type="button"
                className="api-modal-close"
                onClick={() => setPreviewRecord(null)}
                aria-label="关闭"
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                  <path d="M6 6l12 12M18 6L6 18" strokeLinecap="round" />
                </svg>
              </button>
            </header>

            <div className="api-modal-body conv-modal-body conv-md">
              {!previewRecord.ok ? (
                <div className="err-box">{previewRecord.error}</div>
              ) : previewError ? (
                <div className="err-box">{previewError}</div>
              ) : previewLoading || previewContent === null ? (
                <p className="conv-loading">正在读取正文…</p>
              ) : (
                <>
                  {previewContent.length > PREVIEW_CHAR_LIMIT && (
                    <p className="conv-sheet-note">
                      内容较长，此处仅预览前 {PREVIEW_CHAR_LIMIT.toLocaleString()} 字符；
                      完整内容请「复制全文」或「下载」另存到本机。
                    </p>
                  )}
                  <MarkdownContent content={previewContent.slice(0, PREVIEW_CHAR_LIMIT)} />
                </>
              )}
            </div>

            <footer className="api-modal-foot">
              <button
                type="button"
                className="api-cancel"
                onClick={() => setPreviewRecord(null)}
              >
                关闭
              </button>
              <button
                type="button"
                className="api-cancel"
                disabled={!previewRecord.ok}
                onClick={() => void copyRecord(previewRecord)}
              >
                复制全文
              </button>
              <button
                type="button"
                className="api-save"
                disabled={!previewRecord.ok}
                onClick={() => openSaveDialog(previewRecord)}
              >
                下载 .md
              </button>
            </footer>
          </div>
        </div>
      )}

      {saveRecord && (
        <div
          className="api-modal-mask"
          role="presentation"
          onClick={() => !saving && !pickingDir && setSaveRecord(null)}
        >
          <div
            className="api-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="conv-save-title"
            onClick={(event) => event.stopPropagation()}
          >
            <header className="api-modal-head">
              <div>
                <h2 id="conv-save-title">下载 Markdown</h2>
                <p className="api-modal-lead">确认文件名与保存位置，保存目录可以直接输入或调用系统选择框。</p>
              </div>
              <button
                type="button"
                className="api-modal-close"
                onClick={() => setSaveRecord(null)}
                aria-label="关闭"
                disabled={saving || pickingDir}
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                  <path d="M6 6l12 12M18 6L6 18" strokeLinecap="round" />
                </svg>
              </button>
            </header>

            <div className="api-modal-body">
              <div className="field">
                <label htmlFor="conv-save-name">文件名</label>
                <input
                  id="conv-save-name"
                  type="text"
                  value={saveName}
                  maxLength={120}
                  disabled={saving}
                  onChange={(event) => setSaveName(event.target.value)}
                  placeholder="例如：题目.md"
                  autoComplete="off"
                />
                <p className="api-field-hint">将以 .md 保存；如未填写后缀会自动补上。</p>
              </div>

              <div className="field">
                <label htmlFor="conv-save-dir">保存位置</label>
                <div className="conv-dir-row">
                  <input
                    id="conv-save-dir"
                    type="text"
                    value={saveDir}
                    disabled={saving || pickingDir}
                    onChange={(event) => setSaveDir(event.target.value)}
                    placeholder="例如：D:\\论文\\素材"
                    autoComplete="off"
                  />
                  <button
                    type="button"
                    className="api-btn"
                    onClick={() => void pickDirectory()}
                    disabled={saving || pickingDir}
                  >
                    {pickingDir ? '选择中…' : '选择目录'}
                  </button>
                </div>
                <p className="api-field-hint">
                  目录必须已存在；点击「选择目录」会打开系统文件夹选择框。
                </p>
              </div>

              {saveDir.trim() && (
                <p className="conv-save-preview">
                  将保存为：{saveDir.trim().replace(/[\\/]+$/, '')}\
                  {saveName.trim().replace(/\.md$/i, '') || '未命名'}.md
                </p>
              )}
            </div>

            <footer className="api-modal-foot">
              <button
                type="button"
                className="api-cancel"
                onClick={() => setSaveRecord(null)}
                disabled={saving}
              >
                取消
              </button>
              <button
                type="button"
                className="api-save"
                onClick={() => void confirmSave()}
                disabled={saving || !saveDir.trim()}
              >
                {saving ? '保存中…' : '保存'}
              </button>
            </footer>
          </div>
        </div>
      )}
    </div>
  )
}

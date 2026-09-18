import { useEffect } from 'react'

type Props = {
  open: boolean
  title: string
  description?: string
  /** 补充说明区块，例如路径、影响范围 */
  detail?: string
  confirmLabel?: string
  cancelLabel?: string
  /** danger 用于删除等不可逆操作，主按钮为红色 */
  tone?: 'default' | 'danger'
  onConfirm: () => void
  onCancel: () => void
}

/** 与 API 管理编辑弹窗同一套暖黄工作台风格的通用确认框。 */
export default function ConfirmDialog({
  open,
  title,
  description,
  detail,
  confirmLabel = '确定',
  cancelLabel = '取消',
  tone = 'default',
  onConfirm,
  onCancel,
}: Props) {
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCancel()
    }
    window.addEventListener('keydown', onKey)
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = prev
    }
  }, [open, onCancel])

  if (!open) return null

  return (
    <div className="confirm-mask" onClick={onCancel} role="presentation">
      <div
        className="confirm-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-dialog-title"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="confirm-head">
          <div>
            <h2 id="confirm-dialog-title">{title}</h2>
            {description ? <p className="confirm-lead">{description}</p> : null}
          </div>
        </header>
        {detail ? (
          <div className="confirm-body">
            <p className="confirm-detail">{detail}</p>
          </div>
        ) : null}
        <footer className="confirm-foot">
          <button type="button" className="confirm-cancel" onClick={onCancel}>
            {cancelLabel}
          </button>
          <button
            type="button"
            className={`confirm-ok${tone === 'danger' ? ' is-danger' : ''}`}
            onClick={onConfirm}
            autoFocus
          >
            {confirmLabel}
          </button>
        </footer>
      </div>
    </div>
  )
}

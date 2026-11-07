import { useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

type Action = {
  label: string
  onSelect: () => void
  danger?: boolean
  disabled?: boolean
}

/** 资源卡片的次要操作菜单，使用门户避免被滚动容器裁切。 */
export default function MoreActions({ label, actions }: { label: string; actions: Action[] }) {
  const [position, setPosition] = useState<{ top: number; left: number } | null>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const menuId = useId()

  function closeAndFocus() {
    setPosition(null)
    triggerRef.current?.focus()
  }

  useEffect(() => {
    if (!position) return
    menuRef.current?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus()
    function onPointerDown(event: PointerEvent) {
      if (event.target instanceof Node && !menuRef.current?.contains(event.target)
        && !triggerRef.current?.contains(event.target)) setPosition(null)
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== 'Escape') return
      event.preventDefault()
      event.stopPropagation()
      closeAndFocus()
    }
    function onLayoutChange() { setPosition(null) }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown, true)
    window.addEventListener('resize', onLayoutChange)
    window.addEventListener('scroll', onLayoutChange, true)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown, true)
      window.removeEventListener('resize', onLayoutChange)
      window.removeEventListener('scroll', onLayoutChange, true)
    }
  }, [position])

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className="resource-more-trigger"
        aria-label={`${label}的更多操作`}
        aria-haspopup="menu"
        aria-controls={position ? menuId : undefined}
        aria-expanded={position !== null}
        title="更多操作"
        onClick={() => {
          if (position) { closeAndFocus(); return }
          const rect = triggerRef.current!.getBoundingClientRect()
          // 菜单宽 176px，每项预留 44px 热区；靠近视口底部时向上展开。
          const height = actions.length * 44 + 12
          setPosition({
            top: Math.max(8, rect.bottom + height + 6 > window.innerHeight ? rect.top - height - 6 : rect.bottom + 6),
            left: Math.max(8, Math.min(rect.right - 176, window.innerWidth - 184)),
          })
        }}
      >
        <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <circle cx="5" cy="12" r="1.5" /><circle cx="12" cy="12" r="1.5" /><circle cx="19" cy="12" r="1.5" />
        </svg>
      </button>
      {position && createPortal(
        <div
          ref={menuRef}
          id={menuId}
          className="resource-action-menu"
          style={position}
          role="menu"
          aria-label={`${label}的操作`}
          onKeyDown={(event) => {
            if (event.key === 'Tab') { closeAndFocus(); return }
            if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
            event.preventDefault()
            const items = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)'))
            const current = items.indexOf(document.activeElement as HTMLButtonElement)
            const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
              : (current + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length
            items[next]?.focus()
          }}
        >
          {actions.map((action) => (
            <button
              key={action.label}
              type="button"
              role="menuitem"
              className={action.danger ? 'is-danger' : undefined}
              disabled={action.disabled}
              onClick={() => { closeAndFocus(); action.onSelect() }}
            >{action.label}</button>
          ))}
        </div>, document.body,
      )}
    </>
  )
}

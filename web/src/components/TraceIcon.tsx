import { memo } from 'react'

import type { TraceIconName } from '../agentTraceRows'

/**
 * 轨迹行图标：统一 15×15 线性图形，颜色继承当前文字色。
 *
 * 参考 DeepSeek Harness 的轨迹摘要：每行一个图形化动作标签，
 * 图形只负责区分动作类型，不承担状态着色（状态由行 class 控制）。
 */
const ICON_PATHS: Record<TraceIconName, JSX.Element> = {
  start: <path d="M8 5.5v9l8-4.5z" strokeLinejoin="round" />,
  plan: <path d="M4 6h2M4 12h2M4 18h2M9 6h11M9 12h11M9 18h11" strokeLinecap="round" />,
  think: (
    <path
      d="M12 4.5l1.7 4.8 4.8 1.7-4.8 1.7L12 17.5l-1.7-4.8L5.5 11l4.8-1.7z"
      strokeLinejoin="round"
    />
  ),
  terminal: (
    <>
      <path d="M5 7.5l3.5 3.5L5 14.5" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M11.5 15.5h7.5" strokeLinecap="round" />
    </>
  ),
  read: (
    <>
      <path d="M7 3.5h6.5L18.5 8v12.5h-11z" strokeLinejoin="round" />
      <path d="M13 3.5V8h5" strokeLinejoin="round" />
      <path d="M9.5 12h5M9.5 15.5h5" strokeLinecap="round" />
    </>
  ),
  folder: <path d="M3.5 6.5h5l2 2.5h10v9.5h-17z" strokeLinejoin="round" />,
  search: (
    <>
      <circle cx="10.5" cy="10.5" r="5.5" />
      <path d="M14.8 14.8l4.7 4.7" strokeLinecap="round" />
    </>
  ),
  book: (
    <>
      <path d="M4 5.5h6a2.5 2.5 0 0 1 2 2.5v10a2 2 0 0 0-2-2H4z" strokeLinejoin="round" />
      <path d="M20 5.5h-6a2.5 2.5 0 0 0-2 2.5v10a2 2 0 0 1 2-2h6z" strokeLinejoin="round" />
    </>
  ),
  globe: (
    <>
      <circle cx="12" cy="12" r="8" />
      <path d="M4 12h16M12 4c2.4 2.2 3.6 5 3.6 8s-1.2 5.8-3.6 8c-2.4-2.2-3.6-5-3.6-8S9.6 6.2 12 4z" />
    </>
  ),
  write: (
    <>
      <path d="M7 3.5h6.5L18.5 8v5" strokeLinejoin="round" />
      <path d="M13 3.5V8h5" strokeLinejoin="round" />
      <path d="M11 20.5l1-4 7-7 3 3-7 7z" strokeLinejoin="round" />
    </>
  ),
  edit: (
    <>
      <path d="M16 4.5l3.5 3.5-11 11H5v-3.5z" strokeLinejoin="round" />
      <path d="M14 6.5l3.5 3.5" strokeLinecap="round" />
    </>
  ),
  image: (
    <>
      <rect x="3.5" y="5.5" width="17" height="13" rx="2" />
      <circle cx="9" cy="10.5" r="1.6" />
      <path d="M4.5 16.5l4.5-4 4 3.5 3-2.5 3.5 3" strokeLinejoin="round" />
    </>
  ),
  data: (
    <>
      <rect x="3.5" y="5" width="17" height="14" rx="2" />
      <path d="M3.5 10h17M3.5 14.5h17M10 5v14" />
    </>
  ),
  formula: <path d="M17.5 5.5h-11l5.5 6.5-5.5 6.5h11" strokeLinejoin="round" />,
  convert: (
    <>
      <path d="M4.5 8.5h13l-3.5-3.5" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M19.5 15.5h-13l3.5 3.5" strokeLinecap="round" strokeLinejoin="round" />
    </>
  ),
  pause: (
    <>
      <circle cx="12" cy="12" r="8" />
      <path d="M12 7.5V12l3 2" strokeLinecap="round" strokeLinejoin="round" />
    </>
  ),
  tool: (
    <path
      d="M14.5 4.5a4.5 4.5 0 0 0 5.6 5.9l-8.6 8.6a2.4 2.4 0 0 1-3.4-3.4l8.6-8.6a4.5 4.5 0 0 0-2.2-2.5z"
      strokeLinejoin="round"
    />
  ),
}

/** 单个轨迹行图标 */
export const TraceIcon = memo(function TraceIcon({ name }: { name: TraceIconName }) {
  return (
    <svg
      className="agent-trace-icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      aria-hidden="true"
    >
      {ICON_PATHS[name]}
    </svg>
  )
})

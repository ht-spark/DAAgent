import { useEffect, useState } from 'react'
import Sidebar from './components/Sidebar'
import SettingsModal from './components/SettingsModal'
import { syncApiSettingsCache, syncGeneralSettingsCache } from './api'
import { applyUiSettings, loadUiSettings } from './settings'
import NewTask from './pages/NewTask'
import History from './pages/History'
import KnowledgeBaseHub from './pages/KnowledgeBaseHub'
import KnowledgeBase from './pages/KnowledgeBase'
import KnowledgeBaseStats from './pages/KnowledgeBaseStats'
import ChunkEmbedding, { type ChunkEmbeddingOptions } from './pages/ChunkEmbedding'
import ChunkEmbeddingProgress from './pages/ChunkEmbeddingProgress'
import KnowledgeBaseHistory from './pages/KnowledgeBaseHistory'
import DocConverter from './pages/DocConverter'
import TaskExpert from './pages/TaskExpert'
import Skills from './pages/Skills'

type Section = 'modeling' | 'brainstorm' | 'converter' | 'task-expert' | 'skills'
type TaskStep = 'submit' | 'progress' | 'result'
type ModelingView = 'new' | 'history'
type BrainstormView = 'knowledge-hub' | 'knowledge' | 'knowledge-history' | 'stats' | 'chunking' | 'chunking-progress'

/** 顶栏「上一级 / 下一级」导航目标 */
type NavTarget = {
  section: Section
  modelingView?: ModelingView
  brainstormView?: BrainstormView
  statsFromHistory?: boolean
}

const SYMBOLS = ['∫', '∑', '∏', '∂', '∇', '∞', 'π', 'σ', 'μ', 'λ', 'α', 'β', 'γ', 'θ', 'Δ', 'Ω']

/** 当前页在信息架构中的上一级与下一级；无则为 null（箭头禁用） */
function resolveLevelNav(options: {
  section: Section
  modelingView: ModelingView
  brainstormView: BrainstormView
  statsFromHistory: boolean
}): { parent: NavTarget | null; next: NavTarget | null } {
  const { section, modelingView, brainstormView, statsFromHistory } = options

  if (section === 'modeling') {
    if (modelingView === 'history') {
      return {
        parent: { section: 'modeling', modelingView: 'new' },
        next: null,
      }
    }
    return {
      parent: null,
      next: { section: 'modeling', modelingView: 'history' },
    }
  }

  if (section === 'converter' || section === 'task-expert' || section === 'skills') {
    return { parent: null, next: null }
  }

  // 知识库维护链路
  switch (brainstormView) {
    case 'knowledge-hub':
      return {
        parent: null,
        next: { section: 'brainstorm', brainstormView: 'knowledge' },
      }
    case 'knowledge':
      return {
        parent: { section: 'brainstorm', brainstormView: 'knowledge-hub' },
        next: { section: 'brainstorm', brainstormView: 'stats', statsFromHistory: false },
      }
    case 'knowledge-history':
      return {
        parent: { section: 'brainstorm', brainstormView: 'knowledge-hub' },
        next: null,
      }
    case 'stats':
      return {
        parent: statsFromHistory
          ? { section: 'brainstorm', brainstormView: 'knowledge-history' }
          : { section: 'brainstorm', brainstormView: 'knowledge' },
        next: { section: 'brainstorm', brainstormView: 'chunking' },
      }
    case 'chunking':
      return {
        parent: { section: 'brainstorm', brainstormView: 'stats' },
        next: { section: 'brainstorm', brainstormView: 'chunking-progress' },
      }
    case 'chunking-progress':
      return {
        parent: { section: 'brainstorm', brainstormView: 'chunking' },
        next: null,
      }
    default:
      return { parent: null, next: null }
  }
}

const NAV_PARENT_TITLE: Record<string, string> = {
  'modeling-new': '新建任务',
  'modeling-history': '历史任务',
  'knowledge-hub': '知识库维护',
  knowledge: '知识库更新',
  'knowledge-history': '知识库统计',
  stats: '资料统计',
  chunking: '分块与嵌入设置',
  'chunking-progress': '执行分块与嵌入',
}

function navTargetKey(target: NavTarget): string {
  if (target.section === 'modeling') return target.modelingView === 'history' ? 'modeling-history' : 'modeling-new'
  if (target.section === 'brainstorm') return target.brainstormView ?? 'knowledge-hub'
  return target.section
}

export default function App() {
  // 入口直接进入新建聊天；历史讨论在侧边栏「最近」中打开
  const [section, setSection] = useState<Section>('modeling')
  const [modelingView, setModelingView] = useState<ModelingView>('new')
  const [brainstormView, setBrainstormView] = useState<BrainstormView>('knowledge-hub')
  const [discussionId, setDiscussionId] = useState<string | null>(null)
  const [taskViewEpoch, setTaskViewEpoch] = useState(0)
  const [activeProjectId, setActiveProjectId] = useState<string | null>(null)
  const [activeKnowledgeBaseId, setActiveKnowledgeBaseId] = useState<string | undefined>()
  // 标记资料统计是从「知识库统计」打开，以便返回正确页面
  const [statsFromHistory, setStatsFromHistory] = useState(false)
  const [chunkEmbeddingOptions, setChunkEmbeddingOptions] = useState<ChunkEmbeddingOptions>({
    strategy: 'semantic',
    chunkSize: 800,
    overlap: 100,
    windowSize: 5,
  })
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [task, setTask] = useState<{ step: TaskStep; runId: string | null }>({
    step: 'submit',
    runId: null,
  })

  // 启动时把后端本地文件中的 API 配置同步进浏览器缓存，
  // 换浏览器/端口或清缓存后无需重新设置。
  useEffect(() => {
    void syncApiSettingsCache()
    void syncGeneralSettingsCache()
    applyUiSettings(loadUiSettings())
  }, [])

  function startNew() {
    setTaskViewEpoch((value) => value + 1)
    setDiscussionId(null)
    setTask({ step: 'submit', runId: null })
    setSection('modeling')
    setModelingView('new')
  }

  function openFromHistory(runId: string) {
    setTaskViewEpoch((value) => value + 1)
    // 所有历史任务都先恢复聊天上下文；ChatModeling 会根据任务状态展示进度或结果。
    setTask({ step: 'progress', runId })
    setSection('modeling')
    setModelingView('new')
  }

  function applyNavTarget(target: NavTarget) {
    setSection(target.section)
    if (target.modelingView) setModelingView(target.modelingView)
    if (target.brainstormView) setBrainstormView(target.brainstormView)
    if (target.statsFromHistory !== undefined) setStatsFromHistory(target.statsFromHistory)
  }

  const levelNav = resolveLevelNav({
    section,
    modelingView,
    brainstormView,
    statsFromHistory,
  })

  return (
    <div className="shell">
      {/* Animated background (shared across all views) */}
      <div className="bg-grid" />
      <div className="bg-glow" />
      <div className="floating-symbols">
        {SYMBOLS.map((s, i) => (
          <span key={i}>{s}</span>
        ))}
      </div>

      <Sidebar
        section={section}
        collapsed={sidebarCollapsed}
        activeDiscussionId={discussionId}
        activeProjectId={activeProjectId}
        onProjectChange={(projectId) => {
          setActiveProjectId(projectId)
          startNew()
        }}
        onHome={() => startNew()}
        onModeling={() => startNew()}
        onBrainstorm={() => {
          // 侧边栏「知识库」直接进入维护总览（知识库更新 / 历史记录），
          // 不再经过单卡片的中转页。
          setSection('brainstorm')
          setBrainstormView('knowledge-hub')
        }}
        onOpenDiscussion={(id, projectId) => {
          setTaskViewEpoch((value) => value + 1)
          setActiveProjectId(projectId)
          setDiscussionId(id)
          setTask({ step: 'submit', runId: null })
          setSection('modeling')
          setModelingView('new')
        }}
        onConverter={() => setSection('converter')}
        onTaskExpert={() => setSection('task-expert')}
        onSkills={() => setSection('skills')}
        onSettings={() => setSettingsOpen(true)}
        onDiscussionDeleted={(id) => {
          if (discussionId === id) setDiscussionId(null)
        }}
      />
      <SettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)} />

      <main className={`content ${sidebarCollapsed ? 'content--collapsed' : ''}`}>
        <div className="content-top-actions">
          <button
            className="page-back-button"
            onClick={() => setSidebarCollapsed((c) => !c)}
            aria-label={sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'}
            title={sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'}
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M3 7h13" />
              <path d="M3 12h18" />
              <path d="M3 17h13" />
            </svg>
          </button>
          <div className="page-level-nav" role="navigation" aria-label="页面层级导航">
            <button
              type="button"
              className="page-level-nav-btn"
              disabled={!levelNav.parent}
              onClick={() => levelNav.parent && applyNavTarget(levelNav.parent)}
              aria-label="上一级"
              title={levelNav.parent ? `上一级：${NAV_PARENT_TITLE[navTargetKey(levelNav.parent)] ?? '返回'}` : '没有上一级'}
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M19 12H5" />
                <path d="m11 18-6-6 6-6" />
              </svg>
            </button>
            <button
              type="button"
              className="page-level-nav-btn"
              disabled={!levelNav.next}
              onClick={() => levelNav.next && applyNavTarget(levelNav.next)}
              aria-label="下一级"
              title={levelNav.next ? `下一级：${NAV_PARENT_TITLE[navTargetKey(levelNav.next)] ?? '继续'}` : '没有下一级'}
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M5 12h14" />
                <path d="m13 6 6 6-6 6" />
              </svg>
            </button>
          </div>
        </div>
        {section === 'modeling' && modelingView === 'new' && (
          <NewTask
            key={taskViewEpoch}
            task={task}
            setTask={setTask}
            onHistory={() => setModelingView('history')}
            discussionId={discussionId}
            projectId={activeProjectId}
            onDiscussionId={setDiscussionId}
            onManageSkills={() => setSection('skills')}
          />
        )}
        {section === 'brainstorm' && brainstormView === 'knowledge-hub' && (
          <KnowledgeBaseHub
            onUpdate={() => {
              setBrainstormView('knowledge')
            }}
            onHistory={() => setBrainstormView('knowledge-history')}
          />
        )}
        {section === 'brainstorm' && brainstormView === 'knowledge' && (
          <KnowledgeBase
            initialKnowledgeBaseId={activeKnowledgeBaseId}
            onKnowledgeBaseIdChange={setActiveKnowledgeBaseId}
            onNext={() => {
              setStatsFromHistory(false)
              setBrainstormView('stats')
            }}
          />
        )}
        {section === 'brainstorm' && brainstormView === 'knowledge-history' && (
          <KnowledgeBaseHistory
            onOpenKnowledgeBase={(knowledgeBaseId) => {
              // 详细文件记录统一放在资料统计；维护页每次打开为空
              setActiveKnowledgeBaseId(knowledgeBaseId)
              setStatsFromHistory(true)
              setBrainstormView('stats')
            }}
          />
        )}
        {section === 'brainstorm' && brainstormView === 'stats' && (
          <KnowledgeBaseStats
            knowledgeBaseId={activeKnowledgeBaseId}
            onBack={() => setBrainstormView(statsFromHistory ? 'knowledge-history' : 'knowledge')}
            onNext={() => setBrainstormView('chunking')}
          />
        )}
        {section === 'brainstorm' && brainstormView === 'chunking' && (
          <ChunkEmbedding
            knowledgeBaseId={activeKnowledgeBaseId}
            onBack={() => setBrainstormView('stats')}
            onNext={(options) => {
              setChunkEmbeddingOptions(options)
              setBrainstormView('chunking-progress')
            }}
            initialOptions={chunkEmbeddingOptions}
          />
        )}
        {section === 'brainstorm' && brainstormView === 'chunking-progress' && (
          <ChunkEmbeddingProgress
            options={chunkEmbeddingOptions}
            knowledgeBaseId={activeKnowledgeBaseId}
            onBack={() => setBrainstormView('chunking')}
          />
        )}
        {section === 'modeling' && modelingView === 'history' && <History onOpen={openFromHistory} />}
        {section === 'converter' && <DocConverter />}
        {section === 'task-expert' && <TaskExpert />}
        {section === 'skills' && <Skills />}
      </main>
    </div>
  )
}

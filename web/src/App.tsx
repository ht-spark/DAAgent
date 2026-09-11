import { useCallback, useEffect, useState } from 'react'
import Sidebar from './components/Sidebar'
import SettingsModal from './components/SettingsModal'
import { syncApiSettingsCache } from './api'
import { applyUiSettings, loadUiSettings } from './settings'
import NewTask from './pages/NewTask'
import History from './pages/History'
import ApiManager from './pages/ApiManager'
import DiscussionHistory from './pages/DiscussionHistory'
import Docs from './pages/Docs'
import KnowledgeBaseHub from './pages/KnowledgeBaseHub'
import KnowledgeBase from './pages/KnowledgeBase'
import KnowledgeBaseStats from './pages/KnowledgeBaseStats'
import ChunkEmbedding, { type ChunkEmbeddingOptions } from './pages/ChunkEmbedding'
import ChunkEmbeddingProgress from './pages/ChunkEmbeddingProgress'
import VectorRetrievalReady from './pages/VectorRetrievalReady'
import KnowledgeBaseHistory from './pages/KnowledgeBaseHistory'

type Section = 'modeling' | 'brainstorm' | 'api' | 'docs'
type TaskStep = 'submit' | 'progress' | 'result'
type ModelingView = 'overview' | 'new' | 'history' | 'discussion-history'
type BrainstormView = 'knowledge-hub' | 'knowledge' | 'knowledge-history' | 'stats' | 'chunking' | 'chunking-progress' | 'retrieval-ready'

const SYMBOLS = ['∫', '∑', '∏', '∂', '∇', '∞', 'π', 'σ', 'μ', 'λ', 'α', 'β', 'γ', 'θ', 'Δ', 'Ω']

export default function App() {
  // 入口直接进入新建聊天；历史讨论在侧边栏「最近」中打开
  const [section, setSection] = useState<Section>('modeling')
  const [modelingView, setModelingView] = useState<ModelingView>('new')
  const [brainstormView, setBrainstormView] = useState<BrainstormView>('knowledge-hub')
  const [discussionId, setDiscussionId] = useState<string | null>(null)
  // 知识库上传页的进入方式：新批次（空表开始）或回看历史某次上传批次。
  const [kbEntry, setKbEntry] = useState<{
    startEmpty: boolean
    filterIds?: string[]
    from: BrainstormView
  } | null>(null)
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
    applyUiSettings(loadUiSettings())
  }, [])

  // 知识库上传页展示的文档清单变化时，记录为当前批次的 ID 集合。
  const handleKbVisibleIds = useCallback((documentIds: string[]) => {
    setKbEntry((entry) => (entry ? { ...entry, filterIds: documentIds } : entry))
  }, [])

  function startNew() {
    setDiscussionId(null)
    setTask({ step: 'submit', runId: null })
    setSection('modeling')
    setModelingView('new')
  }

  function openFromHistory(runId: string) {
    // 所有历史任务都先恢复聊天上下文；ChatModeling 会根据任务状态展示进度或结果。
    setTask({ step: 'progress', runId })
    setSection('modeling')
    setModelingView('new')
  }

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
        onHome={() => startNew()}
        onModeling={() => startNew()}
        onBrainstorm={() => {
          // 侧边栏「知识库」直接进入维护总览（知识库更新 / 历史记录），
          // 不再经过单卡片的中转页。
          setSection('brainstorm')
          setBrainstormView('knowledge-hub')
        }}
        onOpenDiscussion={(id) => {
          setDiscussionId(id)
          setTask({ step: 'submit', runId: null })
          setSection('modeling')
          setModelingView('new')
        }}
        onApi={() => setSection('api')}
        onDocs={() => setSection('docs')}
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
        </div>
        {section === 'modeling' && modelingView === 'new' && (
          <NewTask
            task={task}
            setTask={setTask}
            onHistory={() => setModelingView('history')}
            discussionId={discussionId}
            onDiscussionId={setDiscussionId}
            onOpenDiscussionHistory={() => setModelingView('discussion-history')}
            onNewDiscussion={() => setDiscussionId(null)}
          />
        )}
        {section === 'modeling' && modelingView === 'discussion-history' && (
          <DiscussionHistory
            onOpen={(id) => {
              setDiscussionId(id)
              setModelingView('new')
            }}
          />
        )}
        {section === 'brainstorm' && brainstormView === 'knowledge-hub' && (
          <KnowledgeBaseHub
            onUpdate={() => {
              // 每次进入知识库更新都是新批次：上传页从空白开始。
              setKbEntry({ startEmpty: true, from: 'knowledge-hub' })
              setBrainstormView('knowledge')
            }}
            onHistory={() => setBrainstormView('knowledge-history')}
          />
        )}
        {section === 'brainstorm' && brainstormView === 'knowledge' && (
          <KnowledgeBase
            startEmpty={kbEntry?.startEmpty ?? false}
            visibleDocumentIds={kbEntry?.filterIds}
            onVisibleIdsChange={handleKbVisibleIds}
            onNext={() => setBrainstormView('stats')}
          />
        )}
        {section === 'brainstorm' && brainstormView === 'knowledge-history' && (
          <KnowledgeBaseHistory
            onOpenUploadTask={(documentIds) => {
              setKbEntry({ startEmpty: false, filterIds: documentIds, from: 'knowledge-history' })
              setBrainstormView('knowledge')
            }}
          />
        )}
        {section === 'brainstorm' && brainstormView === 'stats' && (
          <KnowledgeBaseStats
            visibleDocumentIds={kbEntry?.filterIds}
            onBack={() => setBrainstormView('knowledge')}
            onNext={() => setBrainstormView('chunking')}
          />
        )}
        {section === 'brainstorm' && brainstormView === 'chunking' && (
          <ChunkEmbedding
            visibleDocumentIds={kbEntry?.filterIds}
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
            visibleDocumentIds={kbEntry?.filterIds}
            onBack={() => setBrainstormView('chunking')}
            onNext={() => setBrainstormView('retrieval-ready')}
          />
        )}
        {section === 'brainstorm' && brainstormView === 'retrieval-ready' && (
          <VectorRetrievalReady onBack={() => setBrainstormView('chunking-progress')} />
        )}
        {section === 'modeling' && modelingView === 'history' && <History onOpen={openFromHistory} />}
        {section === 'api' && <ApiManager />}
        {section === 'docs' && <Docs />}
      </main>
    </div>
  )
}

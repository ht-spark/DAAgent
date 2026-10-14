import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'

import {
  createDraftFromExpert,
  createExpertDraft,
  deleteExpert,
  deleteExpertDraft,
  listExpertDrafts,
  listExperts,
  listSkills,
  publishExpertDraft,
  updateExpertDraft,
  validateExpertDraft,
  type ExpertDraft,
  type ExpertInfo,
  type ExpertSkillBinding,
  type ExpertSopStage,
  type SkillInfo,
} from '../api'
import ConfirmDialog from '../components/ConfirmDialog'
import { pushModalLayer } from '../modalLayer'
import { SkillDetailDialog } from './Skills'

type Props = {
  view: TaskExpertView
  onSelect: (expertId: string) => void
  onEditorOpen: () => void
  onEditorComplete: () => void
}

export type TaskExpertView = 'list' | 'editor'

type EditorState = {
  draft: ExpertDraft
  persisted: boolean
}

/** 详情弹窗展示的专家视图：已发布版本与草稿共用同一结构 */
type ExpertDetailView = {
  expert: ExpertDraft
  statusLabel: string
}

const EMPTY_DRAFT: ExpertDraft = {
  expert_id: '',
  name: '',
  description: '',
  version: '1.0.0',
  enabled: true,
  glyph: '专',
  tags: [],
  instructions: '',
  skill_bindings: [],
  sop_stages: [],
  default_skill_ids: [],
  output_requirements: [],
  validation_requirements: [],
}

/** 编辑器板块标识：身份与职责、业务 SOP、Skills 装配、交付与验证。 */
type EditorSectionKey = 'identity' | 'sop' | 'skills' | 'delivery'

/** 板块完成度状态：empty 未填写、partial 待完善、ready 已就绪。 */
type SectionStatus = 'empty' | 'partial' | 'ready'

/** 编辑器板块清单：顺序即展示顺序，标题与说明同时用于总览卡片和区块头。 */
const EDITOR_SECTIONS: Array<{ key: EditorSectionKey; title: string; description: string }> = [
  {
    key: 'identity',
    title: '身份与职责',
    description: '定义专家是谁、适用什么场景，以及它应如何判断和行动。',
  },
  {
    key: 'sop',
    title: '业务 SOP',
    description: '阶段是给模型的业务指引，不锁死建模方法；不适用时模型可说明原因后跳过。',
  },
  {
    key: 'skills',
    title: 'Skills 装配',
    description: '勾选支撑业务 SOP 所需的能力；已选择的 Skill 会在专家运行时自动加载，未选择的不会加载。',
  },
  {
    key: 'delivery',
    title: '交付与验证',
    description: '规定最终产物必须包含什么，以及提交前必须检查什么。',
  },
]

/** 板块完成度状态徽标文案。 */
const SECTION_STATUS_LABELS: Record<SectionStatus, string> = {
  empty: '未填写',
  partial: '待完善',
  ready: '已就绪',
}

/** 管理专家草稿、Skill 装配和发布版本。 */
export default function TaskExpert({
  view,
  onSelect,
  onEditorOpen,
  onEditorComplete,
}: Props) {
  const [experts, setExperts] = useState<ExpertInfo[]>([])
  const [drafts, setDrafts] = useState<ExpertDraft[]>([])
  const [skills, setSkills] = useState<SkillInfo[]>([])
  const [editor, setEditor] = useState<EditorState | null>(null)
  const [pendingDelete, setPendingDelete] = useState<ExpertDraft | null>(null)
  const [pendingExpertDelete, setPendingExpertDelete] = useState<ExpertInfo | null>(null)
  // 当前在详情弹窗中查看的专家（已发布或草稿均可查看）
  const [viewing, setViewing] = useState<ExpertDetailView | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  async function refresh() {
    setLoading(true)
    setError('')
    try {
      const [publishedItems, draftItems, skillItems] = await Promise.all([
        listExperts(),
        listExpertDrafts(),
        listSkills(),
      ])
      setExperts(publishedItems)
      setDrafts(draftItems)
      setSkills(skillItems)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '获取专家配置失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void refresh()
  }, [])

  function openEditor(draft: ExpertDraft, persisted: boolean) {
    setError('')
    setNotice('')
    setEditor({ draft: normalizeDraft(draft), persisted })
    onEditorOpen()
  }

  async function configureExpert(expert: ExpertInfo) {
    setBusy(true)
    setError('')
    try {
      openEditor(await createDraftFromExpert(expert.expert_id), true)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法创建专家草稿')
    } finally {
      setBusy(false)
    }
  }

  async function refreshListsOnly() {
    const [publishedItems, draftItems] = await Promise.all([
      listExperts(),
      listExpertDrafts(),
    ])
    setExperts(publishedItems)
    setDrafts(draftItems)
  }

  async function saveCurrentDraft(): Promise<ExpertDraft | null> {
    if (!editor) return null
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const saved = editor.persisted
        ? await updateExpertDraft(editor.draft.expert_id, editor.draft)
        : await createExpertDraft(editor.draft)
      setEditor({ draft: saved, persisted: true })
      setNotice('草稿已保存')
      await refreshListsOnly()
      return saved
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存专家草稿失败')
      return null
    } finally {
      setBusy(false)
    }
  }

  async function validateCurrentDraft() {
    const saved = await saveCurrentDraft()
    if (!saved) return
    setBusy(true)
    setError('')
    try {
      const result = await validateExpertDraft(saved.expert_id)
      const skillSummary = result.runtime_skill_ids.length
        ? `运行时将自动加载 ${result.runtime_skill_ids.length} 个 Skill`
        : '运行时不自动加载 Skill'
      setNotice(`配置校验通过，${skillSummary}`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '专家配置校验失败')
    } finally {
      setBusy(false)
    }
  }

  async function publishCurrentDraft() {
    const saved = await saveCurrentDraft()
    if (!saved) return
    setBusy(true)
    setError('')
    try {
      await publishExpertDraft(saved.expert_id)
      await refreshListsOnly()
      setEditor(null)
      onEditorComplete()
      setNotice(`「${saved.name}」${saved.version} 已发布`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '发布专家失败')
    } finally {
      setBusy(false)
    }
  }

  async function confirmDelete() {
    if (!pendingDelete || busy) return
    setBusy(true)
    setError('')
    setNotice('')
    try {
      await deleteExpertDraft(pendingDelete.expert_id)
      if (editor?.draft.expert_id === pendingDelete.expert_id) setEditor(null)
      await refreshListsOnly()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '删除专家草稿失败')
    } finally {
      setPendingDelete(null)
      setBusy(false)
    }
  }

  async function confirmDeleteExpert() {
    if (!pendingExpertDelete || busy) return
    setBusy(true)
    setError('')
    setNotice('')
    try {
      await deleteExpert(pendingExpertDelete.expert_id)
      await refreshListsOnly()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '删除专家失败')
    } finally {
      setPendingExpertDelete(null)
      setBusy(false)
    }
  }

  if (view === 'editor' && editor) {
    return (
      <ExpertEditor
        state={editor}
        skills={skills}
        busy={busy}
        error={error}
        notice={notice}
        onChange={(draft) => setEditor((current) => current ? { ...current, draft } : null)}
        onSave={() => void saveCurrentDraft()}
        onValidate={() => void validateCurrentDraft()}
        onPublish={() => void publishCurrentDraft()}
      />
    )
  }

  return (
    <div className="page modeling-tasks-page task-expert-page">
      <div className="page-head modeling-tasks-head expert-page-head">
        <div className="expert-page-title-block">
          <h1 className="page-title">任务专家</h1>
          <p className="page-sub">把业务角色、业务 SOP 和 Skills 组合成可复用的分析能力。</p>
          {!loading && (
            <div className="expert-page-summary" aria-label="专家配置概览">
              <span><strong>{experts.length}</strong> 个已发布版本</span>
              <span><strong>{drafts.length}</strong> 个待编辑草稿</span>
            </div>
          )}
        </div>
        <button
          type="button"
          className="expert-primary-button"
          onClick={() => openEditor({ ...EMPTY_DRAFT }, false)}
        >
          <PlusIcon />
          新建专家
        </button>
      </div>

      {error ? <p className="skills-error" role="alert">{error}</p> : null}
      {notice ? <p className="expert-notice" role="status">{notice}</p> : null}
      {loading ? <p className="skills-empty">正在读取专家配置…</p> : null}

      {!loading ? (
        <>
          <section className="expert-section" aria-labelledby="published-experts-title">
            <div className="expert-section-head">
              <div>
                <h2 id="published-experts-title">已发布</h2>
                <p>只有这里的版本可以加载到新任务，运行后会固定版本快照。</p>
              </div>
              <span>{experts.length}</span>
            </div>
            {experts.length === 0 ? (
              <div className="expert-empty-state">还没有已发布专家，先新建并完成配置校验。</div>
            ) : (
              <ul className="expert-list">
                {experts.map((expert) => (
                  <li className="expert-list-item" key={expert.expert_id}>
                    <span className="expert-card-avatar" aria-hidden="true">{expert.glyph}</span>
                    <div className="expert-list-main">
                      <div className="expert-list-title-row">
                        <div>
                          <strong>{expert.name}</strong>
                          <span className="expert-list-status">已发布</span>
                        </div>
                      </div>
                      <p>{expert.description}</p>
                      <div className="expert-card-tags">
                        {expert.tags.map((tag) => <span key={tag} className="expert-card-tag">{tag}</span>)}
                        <span className="expert-card-tag is-neutral">{countBoundSkills(expert)} 个 Skill</span>
                        <span className="expert-card-tag is-neutral">{expert.sop_stages.length} 个 SOP 阶段</span>
                      </div>
                    </div>
                    <div className="expert-list-controls">
                      <span className="expert-list-version">版本 {expert.version}</span>
                      <div className="expert-list-actions">
                        <button type="button" onClick={() => setViewing({ expert, statusLabel: '已发布' })}>查看</button>
                        <button type="button" className="is-primary" onClick={() => void configureExpert(expert)} disabled={busy}>编辑</button>
                        <button type="button" className="is-danger" onClick={() => setPendingExpertDelete(expert)}>删除</button>
                      </div>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="expert-section" aria-labelledby="draft-experts-title">
            <div className="expert-section-head">
              <div>
                <h2 id="draft-experts-title">草稿</h2>
                <p>草稿不会影响已发布版本，可持续调整后再发布。</p>
              </div>
              <span>{drafts.length}</span>
            </div>
            {drafts.length === 0 ? (
              <div className="expert-empty-state">暂无待编辑草稿。</div>
            ) : (
              <ul className="expert-draft-list">
                {drafts.map((draft) => (
                  <li className="expert-draft-item" key={draft.expert_id}>
                    <div>
                      <div className="expert-draft-title-row">
                        <strong>{draft.name}</strong>
                        <span>草稿</span>
                      </div>
                      <span>{draft.expert_id} · v{draft.version}</span>
                    </div>
                    <div className="expert-list-actions">
                      <button type="button" onClick={() => setViewing({ expert: draft, statusLabel: '草稿' })}>查看</button>
                      <button type="button" className="is-primary" onClick={() => openEditor(draft, true)}>继续编辑</button>
                      <button type="button" className="is-danger" onClick={() => setPendingDelete(draft)}>删除</button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      ) : null}

      <ConfirmDialog
        open={pendingDelete !== null}
        title={`删除草稿「${pendingDelete?.name ?? ''}」`}
        description="只会删除未发布草稿，已发布版本和历史运行不受影响。"
        detail={pendingDelete ? `${pendingDelete.expert_id} · v${pendingDelete.version}` : undefined}
        confirmLabel={busy ? '正在删除…' : '删除草稿'}
        tone="danger"
        onConfirm={() => void confirmDelete()}
        onCancel={() => setPendingDelete(null)}
      />

      <ConfirmDialog
        open={pendingExpertDelete !== null}
        title={`删除专家「${pendingExpertDelete?.name ?? ''}」`}
        description="将移除该已发布专家及其版本记录与同名草稿；删除后无法再被新任务选择，已完成的运行不受影响。"
        detail={pendingExpertDelete ? `${pendingExpertDelete.expert_id} · v${pendingExpertDelete.version}` : undefined}
        confirmLabel={busy ? '正在删除…' : '删除专家'}
        tone="danger"
        onConfirm={() => void confirmDeleteExpert()}
        onCancel={() => setPendingExpertDelete(null)}
      />

      {viewing ? (
        <ExpertDetailDialog
          view={viewing}
          skills={skills}
          onClose={() => setViewing(null)}
        />
      ) : null}
    </div>
  )
}

/** 专家详情弹窗：只读展示已发布专家或草稿的完整配置。 */
function ExpertDetailDialog({
  view,
  skills,
  onClose,
}: {
  view: ExpertDetailView
  skills: SkillInfo[]
  onClose: () => void
}) {
  // onClose 为内联箭头函数，用 ref 取最新值，避免每次渲染重新注册模态层
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  // 压入模态层：统一处理滚动锁，且 Esc 只作用于最上层弹窗
  useEffect(() => pushModalLayer(() => onCloseRef.current()), [])

  const expert = view.expert
  // skill_id 到名称的映射，未匹配到时回退展示 id
  const skillNames = useMemo(
    () => new Map(skills.map((skill) => [skill.skill_id, skill.name])),
    [skills],
  )

  return (
    <div className="expert-detail-mask" onClick={onClose} role="presentation">
      <div
        className="expert-detail-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="expert-detail-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="expert-detail-head">
          <span className="expert-card-avatar" aria-hidden="true">{expert.glyph || '专'}</span>
          <div className="expert-detail-head-main">
            <div className="expert-detail-title-row">
              <h2 id="expert-detail-title">{expert.name || '未命名专家'}</h2>
              <span className={`expert-detail-status${view.statusLabel === '已发布' ? ' is-published' : ''}`}>
                {view.statusLabel}
              </span>
            </div>
            <p className="expert-detail-meta">{expert.expert_id || 'expert-id'} · v{expert.version}</p>
          </div>
          <button type="button" className="expert-detail-close" onClick={onClose}>关闭</button>
        </header>

        <div className="expert-detail-body">
          <section className="expert-detail-section">
            <h3>简介与标签</h3>
            <p>{expert.description || '未填写简介。'}</p>
            <div className="expert-card-tags">
              {expert.tags.map((tag) => <span key={tag} className="expert-card-tag">{tag}</span>)}
              <span className="expert-card-tag is-neutral">{countBoundSkills(expert)} 个 Skill</span>
              <span className="expert-card-tag is-neutral">{expert.sop_stages.length} 个 SOP 阶段</span>
            </div>
          </section>

          {expert.instructions ? (
            <section className="expert-detail-section">
              <h3>职责与行为准则</h3>
              <p className="is-prewrap">{expert.instructions}</p>
            </section>
          ) : null}

          <section className="expert-detail-section">
            <h3>Skills 装配</h3>
            {expert.skill_bindings.length === 0 ? (
              <p className="expert-detail-empty">未装配 Skill。</p>
            ) : (
              <ul className="expert-detail-card-list">
                {expert.skill_bindings.map((binding) => (
                  <li key={binding.skill_id}>
                    <div className="expert-detail-card-title">
                      <strong>{skillNames.get(binding.skill_id) ?? binding.skill_id}</strong>
                      <span className="expert-detail-mode">自动加载</span>
                    </div>
                    {binding.reason ? <p>{binding.reason}</p> : null}
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="expert-detail-section">
            <h3>业务 SOP</h3>
            {expert.sop_stages.length === 0 ? (
              <p className="expert-detail-empty">未配置 SOP 阶段。</p>
            ) : (
              <ol className="expert-detail-card-list">
                {expert.sop_stages.map((stage, index) => (
                  <li key={`${stage.stage_id}-${index}`}>
                    <div className="expert-detail-card-title">
                      <strong>{index + 1}. {stage.name || stage.stage_id || '未命名阶段'}</strong>
                    </div>
                    {stage.objective ? <p>{stage.objective}</p> : null}
                    <DetailList title="执行指引" items={stage.guidance} />
                    <DetailList title="所需输入" items={stage.required_inputs} />
                    <DetailList title="阻断条件" items={stage.blocking_conditions} />
                  </li>
                ))}
              </ol>
            )}
          </section>

          <section className="expert-detail-section">
            <h3>交付与验证</h3>
            {expert.output_requirements.length === 0 && expert.validation_requirements.length === 0 ? (
              <p className="expert-detail-empty">未配置交付与验证要求。</p>
            ) : (
              <>
                <DetailList title="交付要求" items={expert.output_requirements} />
                <DetailList title="验证要求" items={expert.validation_requirements} />
              </>
            )}
          </section>
        </div>
      </div>
    </div>
  )
}

/** 详情弹窗内的小节列表：带小标题的条目列表，为空时不渲染。 */
function DetailList({ title, items }: { title: string; items: string[] }) {
  if (items.length === 0) return null
  return (
    <div className="expert-detail-sublist">
      <span>{title}</span>
      <ul>
        {items.map((item, index) => <li key={index}>{item}</li>)}
      </ul>
    </div>
  )
}

/** 专家编辑工作区，按职责、技能、SOP、交付约束分组。 */
function ExpertEditor({
  state,
  skills,
  busy,
  error,
  notice,
  onChange,
  onSave,
  onValidate,
  onPublish,
}: {
  state: EditorState
  skills: SkillInfo[]
  busy: boolean
  error: string
  notice: string
  onChange: (draft: ExpertDraft) => void
  onSave: () => void
  onValidate: () => void
  onPublish: () => void
}) {
  const draft = state.draft
  const [detailSkill, setDetailSkill] = useState<SkillInfo | null>(null)
  // 当前聚焦的编辑板块；为空时展示板块卡片总览
  const [activeSection, setActiveSection] = useState<EditorSectionKey | null>(null)
  const selectedSkills = useMemo(
    () => new Map(draft.skill_bindings.map((binding) => [binding.skill_id, binding])),
    [draft.skill_bindings],
  )
  // 当前聚焦板块的元信息，含标题与说明
  const activeMeta = activeSection === null
    ? null
    : EDITOR_SECTIONS.find((section) => section.key === activeSection) ?? null

  function patch(values: Partial<ExpertDraft>) {
    onChange({ ...draft, ...values })
  }

  function toggleSkill(skillId: string, selected: boolean) {
    const next = selected
      ? [...draft.skill_bindings, { skill_id: skillId, reason: '' }]
      : draft.skill_bindings.filter((item) => item.skill_id !== skillId)
    patch({ skill_bindings: next })
  }

  function updateBinding(skillId: string, values: Partial<ExpertSkillBinding>) {
    patch({
      skill_bindings: draft.skill_bindings.map((item) => (
        item.skill_id === skillId ? { ...item, ...values } : item
      )),
    })
  }

  function updateStage(index: number, values: Partial<ExpertSopStage>) {
    patch({
      sop_stages: draft.sop_stages.map((stage, currentIndex) => (
        currentIndex === index ? { ...stage, ...values } : stage
      )),
    })
  }

  /** 打开指定板块的详细编辑，并回到页面顶部。 */
  function openSection(key: EditorSectionKey) {
    setActiveSection(key)
    window.scrollTo(0, 0)
  }

  /** 返回板块卡片总览。 */
  function backToOverview() {
    setActiveSection(null)
    window.scrollTo(0, 0)
  }

  /** 在 SOP 板块末尾追加一个空阶段。 */
  function addStage() {
    patch({ sop_stages: [...draft.sop_stages, newStage(draft.sop_stages.length)] })
  }

  /** 渲染当前板块的编辑表单主体，仅在详细编辑视图中调用。 */
  function renderSectionBody(key: EditorSectionKey): ReactNode {
    if (key === 'identity') {
      return (
        <div className="expert-form-grid">
          <Field label="专家标识" hint="系统内唯一且发布后不可修改；使用小写字母、数字和短横线，如 operations-analysis。">
            <input value={draft.expert_id} disabled={state.persisted} placeholder="operations-analysis" onChange={(event) => patch({ expert_id: event.target.value })} />
          </Field>
          <Field label="版本" hint="建议使用“主版本.次版本.修订版本”，如 1.0.0；更新已发布专家时必须提高版本号。">
            <input value={draft.version} placeholder="1.0.0" onChange={(event) => patch({ version: event.target.value })} />
          </Field>
          <Field label="专家名称" hint="面向用户展示的业务角色名称，建议使用“业务领域 + 专家”。">
            <input value={draft.name} placeholder="运营分析专家" onChange={(event) => patch({ name: event.target.value })} />
          </Field>
          <Field label="图标文字" hint="用于专家卡片识别，建议填写 1 个有代表性的中文字符，如“运”。">
            <input value={draft.glyph} maxLength={4} onChange={(event) => patch({ glyph: event.target.value })} />
          </Field>
          <Field label="简介" hint="用 1～2 句话说明适用业务、典型任务和不负责的范围，帮助用户判断何时选择该专家。" className="is-wide">
            <textarea rows={3} value={draft.description} placeholder="例如：面向互联网运营场景，负责指标诊断、趋势分析和异常归因；不替代财务审计。" onChange={(event) => patch({ description: event.target.value })} />
          </Field>
          <Field label="标签" hint="填写 3～6 个业务领域或核心能力关键词，使用中文或英文逗号分隔。" className="is-wide">
            <input value={draft.tags.join(', ')} placeholder="运营, 指标分析, 异常诊断" onChange={(event) => patch({ tags: splitComma(event.target.value) })} />
          </Field>
          <Field label="职责与行为准则" hint="明确角色目标、工作范围、判断原则、业务口径、信息不足时的处理方式和禁止事项。" className="is-wide">
            <textarea rows={6} value={draft.instructions} placeholder="例如：先确认指标定义和统计口径；区分事实、推断与假设；不得编造缺失数据。" onChange={(event) => patch({ instructions: event.target.value })} />
          </Field>
        </div>
      )
    }

    if (key === 'sop') {
      return draft.sop_stages.length === 0 ? (
        <div className="expert-empty-state">尚未配置 SOP，添加第一个业务阶段。</div>
      ) : (
        <div className="expert-stage-list">
          {draft.sop_stages.map((stage, index) => (
            <article className="expert-stage" key={`${stage.stage_id}-${index}`}>
              <div className="expert-stage-head">
                <span>{index + 1}</span>
                <strong>{stage.name || '未命名阶段'}</strong>
                <button type="button" onClick={() => patch({ sop_stages: draft.sop_stages.filter((_, currentIndex) => currentIndex !== index) })}>移除</button>
              </div>
              <div className="expert-form-grid">
                <Field label="阶段标识" hint="阶段内唯一；使用小写字母、数字和短横线，如 validate-metrics。"><input value={stage.stage_id} placeholder="validate-metrics" onChange={(event) => updateStage(index, { stage_id: event.target.value })} /></Field>
                <Field label="阶段名称" hint="使用清晰的业务动作命名，让用户一眼看懂这一阶段要做什么。"><input value={stage.name} placeholder="确认指标口径" onChange={(event) => updateStage(index, { name: event.target.value })} /></Field>
                <Field label="阶段目标" hint="描述该阶段结束时应得到的明确结果，不要只重复阶段名称。" className="is-wide"><textarea rows={2} value={stage.objective} placeholder="例如：确认核心指标的计算方式、统计范围、时间粒度和去重规则。" onChange={(event) => updateStage(index, { objective: event.target.value })} /></Field>
                <Field label="执行指引" hint="每行一条，按执行顺序写具体动作。"><textarea rows={4} value={stage.guidance.join('\n')} placeholder={'识别任务涉及的核心指标\n确认指标的分子、分母和过滤条件'} onChange={(event) => updateStage(index, { guidance: splitLines(event.target.value) })} /></Field>
                <Field label="所需输入" hint="每行一条，填写进入该阶段前需要具备的数据或业务信息。"><textarea rows={4} value={stage.required_inputs.join('\n')} placeholder={'业务指标说明\n数据字段说明\n统计周期'} onChange={(event) => updateStage(index, { required_inputs: splitLines(event.target.value) })} /></Field>
                <Field label="阻断条件" hint="每行一条，说明何时必须停止并向用户追问，条件应具体且可判断。" className="is-wide"><textarea rows={3} value={stage.blocking_conditions.join('\n')} placeholder={'核心指标存在多个口径但无法确定使用哪一个\n缺少计算核心指标所需的关键字段'} onChange={(event) => updateStage(index, { blocking_conditions: splitLines(event.target.value) })} /></Field>
              </div>
            </article>
          ))}
        </div>
      )
    }

    if (key === 'skills') {
      return (
        <>
          {skills.length === 0 ? (
            <div className="expert-empty-state">当前没有已安装 Skill，可先在“技能”页面导入。</div>
          ) : (
            <ul className="skills-grid expert-skill-grid">
              {skills.map((skill) => {
                const binding = selectedSkills.get(skill.skill_id)
                return (
                  <li className={`skill-card expert-skill-card${binding ? ' is-loaded' : ''}`} key={skill.skill_id}>
                    <span className="skill-card-name" title={skill.name}>{skill.name}</span>
                    <span className="skill-card-actions">
                      <button type="button" className="skill-action" onClick={() => setDetailSkill(skill)}>
                        查看详情
                      </button>
                      <label className="skill-action expert-skill-load-toggle">
                        <input
                          type="checkbox"
                          checked={Boolean(binding)}
                          onChange={(event) => toggleSkill(skill.skill_id, event.target.checked)}
                        />
                        <span>加载</span>
                      </label>
                    </span>
                    {binding ? (
                      <label className="expert-skill-load-note">
                        <span>加载说明</span>
                        <textarea
                          rows={3}
                          value={binding.reason}
                          placeholder="说明该 Skill 支撑哪个 SOP 阶段、主要用途和必要限制"
                          onChange={(event) => updateBinding(skill.skill_id, { reason: event.target.value })}
                        />
                      </label>
                    ) : null}
                  </li>
                )
              })}
            </ul>
          )}
          <p className="expert-mode-help">已勾选的 Skill 均为专家必需能力，并会在每次运行时自动加载。</p>
        </>
      )
    }

    return (
      <div className="expert-form-grid">
        <Field label="交付要求" hint="每行一条，规定最终答案或产物必须包含什么。"><textarea rows={6} value={draft.output_requirements.join('\n')} placeholder={'给出核心结论及对应的数据证据\n说明指标定义、时间范围和分析局限'} onChange={(event) => patch({ output_requirements: splitLines(event.target.value) })} /></Field>
        <Field label="验证要求" hint="每行一条，规定提交结果前必须完成的检查。"><textarea rows={6} value={draft.validation_requirements.join('\n')} placeholder={'检查指标口径与原始数据是否一致\n检查每项核心结论是否有证据支持'} onChange={(event) => patch({ validation_requirements: splitLines(event.target.value) })} /></Field>
      </div>
    )
  }

  return (
    <div className="page expert-editor-page">
      <header className="expert-editor-head">
        <div>
          <h1>{state.persisted ? `编辑 ${draft.name || '专家草稿'}` : '新建专家'}</h1>
          <p>保存草稿后先校验依赖，再发布为任务可加载的固定版本。</p>
        </div>
        <div className="expert-editor-actions">
          <button type="button" onClick={onSave} disabled={busy}>{busy ? '处理中…' : '保存草稿'}</button>
          <button type="button" onClick={onValidate} disabled={busy}>校验配置</button>
          <button type="button" className="is-primary" onClick={onPublish} disabled={busy}>发布版本</button>
        </div>
      </header>

      {error ? <p className="skills-error" role="alert">{error}</p> : null}
      {notice ? <p className="expert-notice" role="status">{notice}</p> : null}

      <div className="expert-editor-layout">
        <main className="expert-editor-main">
          {activeMeta === null ? (
            <div className="expert-overview">
              <p className="expert-overview-hint">
                点击卡片进入对应板块的详细编辑，进入后可通过顶部标签在板块之间快速切换。
              </p>
              <div className="expert-overview-grid">
                {EDITOR_SECTIONS.map((section, index) => {
                  const progress = describeSection(draft, section.key)
                  return (
                    <button
                      key={section.key}
                      type="button"
                      className="expert-overview-card"
                      onClick={() => openSection(section.key)}
                    >
                      <span className="expert-overview-top">
                        <span className="expert-overview-index">{String(index + 1).padStart(2, '0')}</span>
                        <span className="expert-overview-rule" aria-hidden="true" />
                        <span className={`expert-overview-status is-${progress.status}`}>
                          {SECTION_STATUS_LABELS[progress.status]}
                        </span>
                      </span>
                      <strong className="expert-overview-name">{section.title}</strong>
                      <span className="expert-overview-desc">{section.description}</span>
                      <span className="expert-overview-foot">
                        <span className="expert-overview-meta">{progress.meta}</span>
                        <span className="expert-overview-enter">
                          进入编辑
                          <ChevronRightIcon />
                        </span>
                      </span>
                    </button>
                  )
                })}
              </div>
            </div>
          ) : (
            <div className="expert-focus" key={activeMeta.key}>
              <nav className="expert-focus-nav" aria-label="专家配置板块">
                <button type="button" className="expert-focus-back" onClick={backToOverview}>
                  <ChevronLeftIcon />
                  全部板块
                </button>
                <div className="expert-focus-tabs">
                  {EDITOR_SECTIONS.map((section) => {
                    const progress = describeSection(draft, section.key)
                    const isActive = section.key === activeMeta.key
                    return (
                      <button
                        key={section.key}
                        type="button"
                        aria-current={isActive ? 'true' : undefined}
                        className={`expert-focus-tab${isActive ? ' is-active' : ''}`}
                        onClick={() => openSection(section.key)}
                      >
                        <span className={`expert-focus-dot is-${progress.status}`} aria-hidden="true" />
                        {section.title}
                      </button>
                    )
                  })}
                </div>
              </nav>
              <EditorSection
                title={activeMeta.title}
                description={activeMeta.description}
                action={activeMeta.key === 'sop' ? (
                  <button type="button" className="expert-secondary-button" onClick={addStage}>添加阶段</button>
                ) : undefined}
              >
                {renderSectionBody(activeMeta.key)}
              </EditorSection>
            </div>
          )}
        </main>

        <aside className="expert-editor-aside">
          <div className="expert-preview">
            <span className="expert-card-avatar" aria-hidden="true">{draft.glyph || '专'}</span>
            <strong>{draft.name || '未命名专家'}</strong>
            <small>{draft.expert_id || 'expert-id'} · v{draft.version || '1.0.0'}</small>
            <p>{draft.description || '补充专家简介后，这里会展示它的适用场景。'}</p>
            <dl>
              <div><dt>Skills</dt><dd>{draft.skill_bindings.length}</dd></div>
              <div><dt>SOP 阶段</dt><dd>{draft.sop_stages.length}</dd></div>
              <div><dt>检查项</dt><dd>{draft.validation_requirements.length}</dd></div>
            </dl>
          </div>
          <label className="expert-enabled-toggle">
            <input type="checkbox" checked={draft.enabled} onChange={(event) => patch({ enabled: event.target.checked })} />
            <span><strong>发布后可使用</strong><small>关闭后仍可发布，但不会出现在任务选择中。</small></span>
          </label>
        </aside>
      </div>

      {detailSkill ? (
        <SkillDetailDialog skill={detailSkill} onClose={() => setDetailSkill(null)} />
      ) : null}
    </div>
  )
}

function EditorSection({ title, description, action, children }: { title: string; description: string; action?: ReactNode; children: ReactNode }) {
  return <section className="expert-editor-section"><header><div><h2>{title}</h2><p>{description}</p></div>{action}</header>{children}</section>
}

function Field({ label, hint, className = '', children }: { label: string; hint?: string; className?: string; children: ReactNode }) {
  return <label className={`expert-field ${className}`}><span>{label}</span>{children}{hint ? <small>{hint}</small> : null}</label>
}

function normalizeDraft(draft: ExpertDraft): ExpertDraft {
  const source = draft as ExpertDraft & {
    bundle_hash?: string
    skill_bindings?: Array<ExpertSkillBinding & { mode?: string }>
  }
  const { bundle_hash: _bundleHash, ...editable } = source
  const migrated = (editable.skill_bindings ?? []).map(({ mode: _mode, ...binding }) => binding)
  for (const skillId of editable.default_skill_ids ?? []) {
    if (!migrated.some((item) => item.skill_id === skillId)) migrated.push({ skill_id: skillId, reason: '' })
  }
  return { ...editable, skill_bindings: migrated, sop_stages: editable.sop_stages ?? [], default_skill_ids: [] }
}

function newStage(index: number): ExpertSopStage {
  return { stage_id: `stage-${index + 1}`, name: '', objective: '', guidance: [], required_inputs: [], blocking_conditions: [] }
}

function splitLines(value: string): string[] {
  return value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean)
}

function splitComma(value: string): string[] {
  return value.split(/[,，]/).map((item) => item.trim()).filter(Boolean)
}

/** 计算板块完成度与统计文案，用于总览卡片和板块切换标签。 */
function describeSection(draft: ExpertDraft, key: EditorSectionKey): { status: SectionStatus; meta: string } {
  if (key === 'identity') {
    // 身份板块的 6 项关键信息：标识、名称、图标、简介、职责准则、标签
    const filled = [
      draft.expert_id.trim(),
      draft.name.trim(),
      draft.glyph.trim(),
      draft.description.trim(),
      draft.instructions.trim(),
      draft.tags.length > 0,
    ].filter(Boolean).length
    return {
      status: filled === 0 ? 'empty' : filled === 6 ? 'ready' : 'partial',
      meta: `已填写 ${filled} / 6 项关键信息`,
    }
  }
  if (key === 'sop') {
    // 阶段写明名称与目标即视为可用，其余细项不阻断完成度
    const total = draft.sop_stages.length
    const described = draft.sop_stages.filter((stage) => stage.name.trim() && stage.objective.trim()).length
    return {
      status: total === 0 ? 'empty' : described === total ? 'ready' : 'partial',
      meta: total === 0 ? '尚未添加阶段' : `${total} 个阶段 · ${described} 个已写明目标`,
    }
  }
  if (key === 'skills') {
    // 加载说明为空时提示补充，但不阻断装配
    const total = draft.skill_bindings.length
    const noted = draft.skill_bindings.filter((binding) => binding.reason.trim()).length
    return {
      status: total === 0 ? 'empty' : noted === total ? 'ready' : 'partial',
      meta: total === 0 ? '尚未装配 Skill' : `已装配 ${total} 个 · ${noted} 个已写加载说明`,
    }
  }
  const outputs = draft.output_requirements.length
  const validations = draft.validation_requirements.length
  return {
    status: outputs + validations === 0 ? 'empty' : outputs > 0 && validations > 0 ? 'ready' : 'partial',
    meta: outputs + validations === 0 ? '尚未填写要求' : `交付 ${outputs} 条 · 验证 ${validations} 条`,
  }
}

function countBoundSkills(expert: ExpertDraft): number {
  return new Set([...expert.default_skill_ids, ...expert.skill_bindings.map((item) => item.skill_id)]).size
}

function PlusIcon() {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
}

function ChevronRightIcon() {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6l6 6-6 6" /></svg>
}

function ChevronLeftIcon() {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 6l-6 6 6 6" /></svg>
}

/**
 * 任务专家：能力卡片墙。
 * 每张卡片对应一类可启动的专家能力；点击行为后续接入真实流程。
 */

type ExpertCard = {
  id: string
  title: string
  alias: string
  description: string
  tags: string[]
  /** 卡片图标（简短字符/符号，与暖色工作台风格一致） */
  glyph: string
}

const EXPERTS: ExpertCard[] = [
  {
    id: 'ab-test',
    title: 'AB测试',
    alias: '实验设计',
    description: '设计实验方案、计算样本量、解读显著性，并给出上线决策建议。',
    tags: ['实验设计', '显著性', '决策建议'],
    glyph: 'AB',
  },
  {
    id: 'competitor',
    title: '竞品分析',
    alias: '对比洞察',
    description: '梳理竞品功能、定位与差异点，输出可执行的竞争策略与机会点。',
    tags: ['竞品对比', '定位', '策略'],
    glyph: '⚔',
  },
  {
    id: 'annual-report',
    title: '年报',
    alias: '年度总结',
    description: '汇总全年指标、里程碑与问题，生成结构完整、数据可追溯的年度报告。',
    tags: ['年度复盘', '指标汇总', '报告生成'],
    glyph: '年',
  },
  {
    id: 'weekly-report',
    title: '周报',
    alias: '周度同步',
    description: '提炼本周进展、风险与下周计划，一键生成清晰可分享的工作周报。',
    tags: ['进展同步', '风险跟踪', '周计划'],
    glyph: '周',
  },
  {
    id: 'daily-report',
    title: '日报',
    alias: '每日速览',
    description: '快速沉淀当日完成事项与阻塞点，生成简洁日报，方便团队同步。',
    tags: ['当日进展', '阻塞项', '速览'],
    glyph: '日',
  },
  {
    id: 'anomaly',
    title: '异动归因',
    alias: '波动诊断',
    description: '定位指标突增突降的驱动因素，按贡献度拆解维度，给出排查结论。',
    tags: ['指标诊断', '维度拆解', '根因'],
    glyph: '!',
  },
  {
    id: 'causal',
    title: '因果推断',
    alias: '因果识别',
    description: '用因果视角区分相关与因果，设计识别策略，评估干预的真实效果。',
    tags: ['因果识别', '干预评估', '策略设计'],
    glyph: '∵',
  },
  {
    id: 'user-behavior',
    title: '用户行为分析',
    alias: '行为洞察',
    description: '刻画用户路径与关键行为，发现流失与转化瓶颈，提出优化建议。',
    tags: ['路径分析', '转化', '留存'],
    glyph: '行',
  },
]

export default function TaskExpert() {
  return (
    <div className="page modeling-tasks-page task-expert-page">
      <div className="page-head modeling-tasks-head">
        <div>
          <h1 className="page-title">任务专家</h1>
          <p className="page-sub">选择一类专家能力，快速启动对应分析任务。</p>
        </div>
      </div>

      <div className="expert-card-grid">
        {EXPERTS.map((expert) => (
          <button
            key={expert.id}
            type="button"
            className="expert-card"
            // 空壳阶段仅呈现入口，点击行为后续接入真实流程
            onClick={() => undefined}
          >
            <div className="expert-card-head">
              <span className="expert-card-avatar" aria-hidden="true">
                {expert.glyph}
              </span>
              <div className="expert-card-identity">
                <strong className="expert-card-title">{expert.title}</strong>
                <span className="expert-card-alias">{expert.alias}</span>
              </div>
            </div>
            <p className="expert-card-desc">{expert.description}</p>
            <div className="expert-card-tags">
              {expert.tags.map((tag) => (
                <span key={tag} className="expert-card-tag">
                  {tag}
                </span>
              ))}
            </div>
          </button>
        ))}
      </div>
    </div>
  )
}

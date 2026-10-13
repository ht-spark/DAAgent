---
name: competitive-analysis
description: Competitive analysis skill for products and markets. Use when user asks to analyze competitors, compare products, generate competitive intelligence reports, assess market positioning, or track competitor pricing/features/strategy. Supports both single-company deep-dives and multi-competitor matrix comparisons.
---

# Competitive Analysis

Conduct systematic competitive analysis across products, pricing, features, market positioning, and strategy.

## When to Use This Skill

Use this skill when the user requests any of the following:

- "帮我分析一下 XX 行业的竞品" / "Analyze competitors in the XX industry"
- "对比 A 公司和 B 公司的产品" / "Compare products from Company A and Company B"
- "生成一份竞品分析报告" / "Generate a competitive analysis report"
- "帮我做竞品情报监控" / "Monitor competitive intelligence"
- Any request involving competitor comparison, market landscape, or product differentiation analysis

## Core Workflow

### Phase 1: Define Scope

1. **Identify the target**: What product/service/industry is being analyzed?
2. **Identify competitors**: Direct competitors, indirect competitors, and potential entrants
3. **Define analysis dimensions**: Select relevant dimensions from the list below
4. **Determine data sources**: User-uploaded files, web search, existing knowledge base, or a combination

### Phase 2: Gather Intelligence

Collect data across the following dimensions:

#### Functional Comparison
- Feature lists and capabilities
- Feature availability (standard vs. premium)
- Unique/special features
- Integration capabilities (APIs, third-party apps)
- User experience and interface design

#### Pricing Strategy
- Pricing tiers and plans
- Per-unit pricing
- Discount structures
- Free trials and freemium models
- Enterprise/B2B pricing
- Bundling strategies

#### Market Positioning
- Target audience segments
- Value proposition
- Brand perception
- Market share estimates
- Geographic presence

#### Technical Architecture
- Technology stack (if inferable)
- Scalability indicators
- Performance benchmarks
- Security/compliance certifications

#### User Feedback
- Review scores (G2, Capterra, App Store, etc.)
- Common praises and complaints
- Feature requests from users
- Support quality indicators

#### Business Strategy
- Recent funding rounds
- M&A activity
- Partnership ecosystem
- Go-to-market approach
- Hiring patterns (indicating growth areas)

### Phase 3: Structure the Analysis

Organize findings into a clear framework:

1. **Executive Summary**: Key takeaways in 3-5 bullet points
2. **Competitive Landscape**: Market map showing positioning of all players
3. **Feature Matrix**: Side-by-side comparison table
4. **Pricing Comparison**: Tier-by-tier pricing table
5. **SWOT Analysis**: Strengths, Weaknesses, Opportunities, Threats for each major competitor
6. **Market Positioning Analysis**: How each competitor positions itself
7. **Strategic Implications**: What the analysis means for the user's business

### Phase 4: Deliver

Produce the final output in the requested format:

- **Markdown Report**: Structured document with tables, headings, and analysis narrative
- **Comparison Matrix**: Tabular format for quick reference
- **Visual Charts**: Market positioning maps, feature comparison charts (when appropriate)

## Analysis Dimensions Reference

The following dimensions can be included based on user needs:

| Dimension | Description | Typical Data Sources |
|-----------|-------------|---------------------|
| Functional | Feature comparison, capabilities | Product websites, docs, reviews |
| Pricing | Pricing tiers, discounts, bundling | Pricing pages, analyst reports |
| Market | Share, segments, growth | Industry reports, public data |
| Technical | Stack, performance, security | Technical blogs, benchmarks |
| User | Reviews, satisfaction, sentiment | Review platforms, forums |
| Business | Funding, M&A, partnerships | News, Crunchbase, LinkedIn |

## Best Practices

- **Be specific**: Cite concrete data points rather than general impressions
- **Distinguish fact from inference**: Clearly label estimates and assumptions
- **Focus on actionable insights**: Don't just list facts — explain what they mean
- **Update regularly**: Competitive landscapes change; note the analysis date
- **Avoid bias**: Present competitors objectively; don't overweight familiar ones

## Data Sources

- **Web Search**: Use for gathering public information about competitors
- **User-Provided Files**: Analyze any uploaded documents, spreadsheets, or reports
- **Knowledge Base**: Check existing knowledge base for prior competitive analysis

## Output Format

Default to a structured Markdown report with:

1. YAML-style metadata header (date, scope, competitors analyzed)
2. Executive summary section
3. Detailed analysis sections organized by dimension
4. Comparison tables using Markdown table syntax
5. Strategic recommendations section

// GENERATED from optomind_ui/stage_registry.py -- do not edit by hand.
export interface StageDef { name: string; label: string; explain: string }
export interface StageGroup { name: string; stages: StageDef[] }
export const STAGE_GROUPS: StageGroup[] = [
  {
    "name": "准备",
    "stages": [
      {
        "name": "query_planner",
        "label": "理解问题",
        "explain": "把研究问题整理成可执行的检索计划"
      },
      {
        "name": "topic_scoped_kb",
        "label": "圈定材料",
        "explain": "从中央材料库选出主题相关存量材料"
      }
    ]
  },
  {
    "name": "文献",
    "stages": [
      {
        "name": "s2_literature_intelligence",
        "label": "检索文献",
        "explain": "联网检索并解析论文全文摘要证据"
      }
    ]
  },
  {
    "name": "结构",
    "stages": [
      {
        "name": "review_lead",
        "label": "设计结构",
        "explain": "规划综述的章节骨架与论证路线"
      },
      {
        "name": "section_coverage",
        "label": "整理证据",
        "explain": "按章节职责归组证据"
      },
      {
        "name": "section_coverage_portfolio",
        "label": "证据组合",
        "explain": "为每章挑选最优证据组合"
      }
    ]
  },
  {
    "name": "论证",
    "stages": [
      {
        "name": "phase3_argument_orchestration",
        "label": "形成论点",
        "explain": "把证据组织成可写的论点链"
      },
      {
        "name": "authoring_revision",
        "label": "章节初稿",
        "explain": "逐章写作并保留最后有效稿"
      },
      {
        "name": "section_coverage_feedback",
        "label": "补充证据",
        "explain": "按审稿反馈补齐缺口证据"
      }
    ]
  },
  {
    "name": "出版",
    "stages": [
      {
        "name": "publication_mainline_enhancement",
        "label": "章节增强",
        "explain": "补充解释应用案例可读性结构"
      },
      {
        "name": "publication_mainline_handoff",
        "label": "交接材料",
        "explain": "汇总全文写作所需材料并交接"
      },
      {
        "name": "publication_mainline_commander",
        "label": "全文编排",
        "explain": "统一章节关系与全文叙事"
      },
      {
        "name": "publication_mainline_staged_completion",
        "label": "补齐前后文",
        "explain": "生成标题结论引言与摘要"
      },
      {
        "name": "article_completion",
        "label": "论文收尾",
        "explain": "补齐论文结构与出版元数据"
      },
      {
        "name": "article_structure_audit",
        "label": "结构体检",
        "explain": "检查论文结构完整性"
      }
    ]
  },
  {
    "name": "视觉",
    "stages": [
      {
        "name": "visual_editor",
        "label": "视觉规划",
        "explain": "规划图表图注与挂载位置"
      },
      {
        "name": "visual_materialization",
        "label": "图表挂载",
        "explain": "生成或挂载经审核的视觉材料"
      }
    ]
  },
  {
    "name": "交付",
    "stages": [
      {
        "name": "research_plan",
        "label": "研究计划",
        "explain": "整理后续研究方向"
      },
      {
        "name": "packaging",
        "label": "打包交接",
        "explain": "整理引用元数据与交接包"
      },
      {
        "name": "latex_publication",
        "label": "编译PDF",
        "explain": "编译并检查最终英文 PDF"
      },
      {
        "name": "chinese_translation",
        "label": "中文翻译",
        "explain": "生成中文版本内容"
      },
      {
        "name": "latex_publication_zh",
        "label": "中文PDF",
        "explain": "编译中文版 PDF"
      },
      {
        "name": "research_plan_publication",
        "label": "计划附件",
        "explain": "整理研究计划附件 PDF"
      }
    ]
  }
]

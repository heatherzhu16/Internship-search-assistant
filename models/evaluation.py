from __future__ import annotations

from pydantic import BaseModel, Field

from candidate_profile import JDConstraint


RUBRIC_VERSION = "v2.9-core-gap-outlier-guard"


class JDInfo(BaseModel):
    company: str = ""
    role: str = ""
    company_type: str = ""
    location: str = ""
    salary: str = ""
    source: str = ""
    application_email: str = ""
    application_reference: str = ""
    responsibilities: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    hard_requirements: list[str] = Field(default_factory=list)
    constraints: list[JDConstraint] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    full_text: str = ""


class DimensionScore(BaseModel):
    score: int
    max_score: int
    score_reason: str = ""
    evidence: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class Evaluation(BaseModel):
    hard_requirements: DimensionScore
    experience_match: DimensionScore
    skills_tools: DimensionScore
    motivation: DimensionScore
    education: DimensionScore
    keyword_evidence: DimensionScore
    hard_gate_result: str
    hard_gate_notes: list[str] = Field(default_factory=list)
    matched_keywords: list[str] = Field(default_factory=list)
    missing_keywords: list[str] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    resume_suggestions: list[str] = Field(default_factory=list)
    recommendation: str
    recommendation_reason: str
    raw_dimension_scores: dict[str, int] = Field(default_factory=dict)
    calibration_notes: list[str] = Field(default_factory=list)


class ResumeRewrite(BaseModel):
    location: str = ""
    original: str = ""
    suggested: str = ""
    rationale: str = ""
    supported_keywords: list[str] = Field(default_factory=list)


class ApplicationMaterials(BaseModel):
    strategy_summary: str = ""
    priority_actions: list[str] = Field(default_factory=list)
    resume_rewrites: list[ResumeRewrite] = Field(default_factory=list)
    chinese_email_subject: str = ""
    chinese_email_body: str = ""
    english_email_subject: str = ""
    english_email_body: str = ""
    boss_message: str = ""
    truthfulness_notes: list[str] = Field(default_factory=list)


WEIGHTS = {
    "hard_requirements": 25,
    "experience_match": 25,
    "skills_tools": 20,
    "motivation": 10,
    "education": 10,
    "keyword_evidence": 10,
}
DIMENSION_NAMES = {
    "hard_requirements": "核心专业必备项",
    "experience_match": "实习、项目与工作内容匹配",
    "skills_tools": "专业技能和工具",
    "motivation": "行业与岗位动机",
    "education": "教育背景",
    "keyword_evidence": "简历关键词及证据充分程度",
}
DIMENSION_GUIDES = {
    "hard_requirements": (
        "完成岗位核心工作的专业能力；即使 JD 未写“必须”也按核心职责判断。"
        "个人到岗、地点和实习期限只由资格门槛判断。"
    ),
    "experience_match": "任务、业务场景、职责深度和成果与岗位工作的相似度。",
    "skills_tools": "在经历、项目或课程中实际使用过的技能和工具。",
    "motivation": "能够证明行业兴趣、岗位选择或持续投入的直接证据。",
    "education": "专业、相关课程和学习训练与岗位知识要求的匹配。",
    "keyword_evidence": "JD 关键词是否得到具体经历或成果支撑，而不是词频。",
}
SCORE_ANCHORS = (
    "0%=无证据；1%-40%=弱相关或间接证据；41%-60%=部分匹配且有明显缺口；"
    "61%-80%=大部分匹配且证据具体；81%-100%=高度直接匹配、证据充分。"
)

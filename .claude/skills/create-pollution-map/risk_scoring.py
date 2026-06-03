#!/usr/bin/env python3
"""Environmental risk scoring engine.

Computes low / medium / high risk levels for enterprises based on:
  - pollution category baseline risk
  - EIA level (环评等级)
  - penalty / complaint / monitoring violation counts (last 2 years only)
  - multi-source data presence
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Date helpers for "last N years" filtering
# ---------------------------------------------------------------------------

_CURRENT_YEAR = datetime.now().year


def _extract_year(date_str: str) -> int:
    """Extract 4-digit year from common Chinese date formats.

    Supports: 2024-05-20, 2024年5月20日, 2024/05/20, 2024.05.20, 2024
    """
    if not date_str:
        return 0
    m = re.search(r"(\d{4})", str(date_str))
    return int(m.group(1)) if m else 0


def _count_recent_records(records: List[dict], years: int = 2) -> int:
    """Count records whose date falls within the last N years (inclusive).

    Example: current year 2026, years=2 → counts 2025 and 2026.
    """
    if not records:
        return 0
    cutoff = _CURRENT_YEAR - years + 1
    return sum(1 for r in records if _extract_year(r.get("date", "")) >= cutoff)


# ---------------------------------------------------------------------------
# Default weights
# ---------------------------------------------------------------------------

DEFAULT_CATEGORY_BASELINE = {
    "环境风险": 20,
    "土壤污染": 25,
    "固废": 22,
    "水环境": 20,
    "大气环境": 18,
    "辐射安全": 15,
    "地下水": 12,
    "噪声": 15,
    "未分类": 10,
}

DEFAULT_EIA_SCORES = {
    "报告书": 0,
    "报告表": 0,
    "登记表": 0,
    "未知": 0,
}

RISK_SOURCE_TYPES = {"penalty", "complaint", "monitoring", "eia", "exposure"}


# ---------------------------------------------------------------------------
# Configurable weights
# ---------------------------------------------------------------------------

@dataclass
class RiskWeights:
    """Configurable weights for risk scoring dimensions."""

    category_baseline: Dict[str, int] = field(
        default_factory=lambda: dict(DEFAULT_CATEGORY_BASELINE)
    )
    multi_source_bonus: int = 5
    penalty_per_record: int = 15
    complaint_per_record: int = 10
    monitoring_violation_per_record: int = 15
    eia_level_scores: Dict[str, int] = field(
        default_factory=lambda: dict(DEFAULT_EIA_SCORES)
    )
    max_score: int = 100

    # thresholds
    high_threshold: int = 60
    medium_threshold: int = 35


# singleton default instance
_DEFAULT_WEIGHTS = RiskWeights()


# ---------------------------------------------------------------------------
# Core scoring
# ---------------------------------------------------------------------------

def compute_risk_score(
    enterprise: dict,
    weights: Optional[RiskWeights] = None,
    all_enterprises: Optional[List[dict]] = None,
) -> dict:
    """Compute risk score and level for a single enterprise.

    Args:
        enterprise: dict with risk-related fields.
        weights: optional custom weights.
        all_enterprises: full list for cross-enterprise source deduplication.

    Returns:
        dict with keys: risk_score (int), risk_level (str), risk_factors (List[str]).
    """
    weights = weights or _DEFAULT_WEIGHTS
    score = 0
    factors = []

    # 0. Source-type base score — penalty enterprises default to medium
    source_type = enterprise.get("source_type", "official")
    if source_type == "penalty":
        score += 35
        factors.append("行政处罚记录（曝光台）")

    # 1. Category baseline — take highest matching category score
    categories = enterprise.get("categories", [])
    if not categories and enterprise.get("category"):
        categories = [enterprise["category"]]

    cat_score = 0
    for cat in categories:
        baseline = weights.category_baseline.get(cat, 10)
        if baseline > cat_score:
            cat_score = baseline
    score += cat_score
    if cat_score >= 25:
        factors.append("高风险类别")

    # 2. EIA level
    eia = enterprise.get("eia_level", "未知")
    eia_score = weights.eia_level_scores.get(eia, 0)
    score += eia_score
    if eia == "报告书":
        factors.append("环评报告书")

    # 3. Penalties (last 2 years only)
    penalty_records = enterprise.get("penalty_records", [])
    penalties = _count_recent_records(penalty_records) if penalty_records else enterprise.get("penalty_count", 0)
    if penalties > 0:
        score += min(penalties * weights.penalty_per_record, 30)
        if penalties >= 3:
            factors.append("多次处罚")
        else:
            factors.append("行政处罚记录")

    # 4. Complaints (last 2 years only)
    complaint_records = enterprise.get("complaint_records", [])
    complaints = _count_recent_records(complaint_records) if complaint_records else enterprise.get("complaint_count", 0)
    if complaints > 0:
        score += min(complaints * weights.complaint_per_record, 20)
        if complaints >= 3:
            factors.append("多次信访投诉")
        else:
            factors.append("信访投诉记录")

    # 5. Monitoring violations (last 2 years only)
    monitoring_records = enterprise.get("monitoring_records", [])
    violations = _count_recent_records(monitoring_records) if monitoring_records else enterprise.get("monitoring_violations", 0)
    if violations > 0:
        score += min(violations * weights.monitoring_violation_per_record, 25)
        if violations >= 3:
            factors.append("多次监测超标")
        else:
            factors.append("监测超标记录")

    # 6. Multi-source bonus
    source_type = enterprise.get("source_type", "official")
    if all_enterprises and source_type in RISK_SOURCE_TYPES:
        name = enterprise.get("name", "")
        district = enterprise.get("district", "")
        distinct_sources = set()
        for e in all_enterprises:
            if e.get("name") == name and e.get("district") == district:
                st = e.get("source_type", "official")
                if st in RISK_SOURCE_TYPES:
                    distinct_sources.add(st)
        if len(distinct_sources) > 1:
            bonus = (len(distinct_sources) - 1) * weights.multi_source_bonus
            score += bonus
            factors.append("多源风险数据")

    # Clamp
    score = max(0, min(score, weights.max_score))

    # Determine level
    if score >= weights.high_threshold:
        level = "high"
    elif score >= weights.medium_threshold:
        level = "medium"
    else:
        level = "low"

    return {
        "risk_score": score,
        "risk_level": level,
        "risk_factors": factors,
    }


def assign_risk_levels(
    enterprises: List[dict], config: Optional[dict] = None
) -> List[dict]:
    """Assign risk scores / levels to all enterprises in-place.

    Reads weights from config["risk_scoring"]["weights"] if available.
    """
    weights = None
    if config and "risk_scoring" in config:
        cfg = config["risk_scoring"]
        if "weights" in cfg:
            w = cfg["weights"]
            weights = RiskWeights(
                category_baseline=w.get(
                    "category_baseline", dict(DEFAULT_CATEGORY_BASELINE)
                ),
                multi_source_bonus=w.get("multi_source_bonus", 5),
                penalty_per_record=w.get("penalty_per_record", 10),
                complaint_per_record=w.get("complaint_per_record", 5),
                monitoring_violation_per_record=w.get(
                    "monitoring_violation_per_record", 8
                ),
                eia_level_scores=w.get("eia_level_scores", dict(DEFAULT_EIA_SCORES)),
                max_score=w.get("max_score", 100),
                high_threshold=w.get("high_threshold", 60),
                medium_threshold=w.get("medium_threshold", 35),
            )

    for ent in enterprises:
        result = compute_risk_score(ent, weights=weights, all_enterprises=enterprises)
        ent.update(result)

    return enterprises

#!/usr/bin/env python3
"""Batch prefill enterprise addresses from POI search with human-review reporting.

For enterprises with empty address:
  1. Try POI search with loose name matching to get candidate addresses
  2. Score candidates and auto-fill high-confidence ones
  3. Generate a human-review report for ambiguous / failed cases
"""

import json
import os
import re
import sys
import time
import urllib.parse
from collections import defaultdict
from typing import List, Tuple, Optional

import requests
import yaml

sys.path.insert(0, ".claude/skills/create-pollution-map")
from utils import load_dotenv, get_map_key

ENV_FILE = ".claude/skills/create-pollution-map/.env"
load_dotenv(ENV_FILE)

API_KEY = get_map_key()
CITY = "杭州"
RATE_LIMIT = 0.15

# Keywords to prefer production facilities over offices
_PROD_KWS = ["厂", "基地", "园区", "工业区", "厂区", "生产", "制造", "处理厂", "发电"]
_OFFICE_KWS = ["大厦", "写字楼", "中心", "商务楼", "公寓", "商业", "综合体"]

# Blacklist POI categories that are clearly not enterprises
_BAD_CATEGORIES = ["购物", "地铁", "公交", "景点", "美食", "酒店"]


def poi_search_candidates(name: str, city: str, district: str, key: str) -> List[dict]:
    """Search POI with multiple query variants via Gaode Maps, return all candidates without strict name matching."""
    queries = []
    # Variant 1: remove parentheses content
    clean = re.sub(r'[（(].*?[）)]', '', name).strip()
    if clean and clean != name:
        queries.append(clean)
    # Variant 2: original name
    queries.append(name)
    # Variant 3: core short name (last 4-8 chars)
    if len(name) > 8:
        queries.append(name[-8:])

    all_candidates = []
    seen_ids = set()

    for q in queries:
        url = (
            f"https://restapi.amap.com/v3/place/text"
            f"?keywords={urllib.parse.quote(q)}"
            f"&city={urllib.parse.quote(city)}"
            f"&offset=20&page=1"
            f"&key={key}"
        )
        try:
            resp = requests.get(url, timeout=10).json()
            if resp.get("status") == "1" and resp.get("pois"):
                for poi in resp["pois"]:
                    poi_id = poi.get("id", "")
                    if poi_id in seen_ids:
                        continue
                    seen_ids.add(poi_id)

                    poi_name = poi.get("name", "")
                    poi_addr = poi.get("address", "").strip()
                    poi_district = poi.get("adname", "")
                    poi_type = poi.get("type", "")

                    if not poi_addr:
                        continue

                    # Skip clearly non-enterprise POIs
                    if any(k in poi_type for k in _BAD_CATEGORIES):
                        continue

                    all_candidates.append({
                        "name": poi_name,
                        "address": poi_addr,
                        "district": poi_district,
                        "category": poi_type,
                        "location": poi.get("location", ""),
                    })
        except Exception as e:
            print(f"    API error for '{q}': {e}")

        time.sleep(RATE_LIMIT)

    return all_candidates


def score_candidate(poi: dict, enterprise_name: str, target_district: str) -> int:
    """Score a POI candidate. Returns 0-100+ score."""
    score = 0
    poi_name = poi["name"]
    poi_addr = poi["address"]
    poi_district = poi["district"]

    # 1. Name similarity (0-40 pts)
    if enterprise_name == poi_name:
        score += 40
    elif enterprise_name in poi_name or poi_name in enterprise_name:
        score += 35
    else:
        # Core keyword overlap
        ent_core = re.sub(r'[（(].*?[）)]', '', enterprise_name).strip()
        # Remove common suffixes
        for suffix in ["有限公司", "有限责任公司", "股份有限公司", "公司"]:
            ent_core = ent_core.replace(suffix, "")
            poi_name = poi_name.replace(suffix, "")

        if ent_core in poi_name or poi_name in ent_core:
            score += 25
        elif len(ent_core) >= 4 and any(c in poi_name for c in ent_core[:4]):
            score += 15

    # 2. Address completeness (0-30 pts)
    if re.search(r'\d+号|\d+幢|\d+层|\d+室|\d+-\d+号', poi_addr):
        score += 30
    elif re.search(r'\d+', poi_addr) and len(poi_addr) > 15:
        score += 20
    elif len(poi_addr) > 10:
        score += 10
    else:
        score += 5

    # 3. District consistency (0-20 pts)
    if target_district and poi_district:
        if target_district in poi_district or poi_district in target_district:
            score += 20
        else:
            score -= 10  # Wrong district is a strong negative signal

    # 4. Address type preference (0-15 pts)
    for kw in _PROD_KWS:
        if kw in poi_addr:
            score += 15
            break
    else:
        for kw in _OFFICE_KWS:
            if kw in poi_addr:
                score -= 10
                break

    return max(0, score)


def select_best_address(candidates: List[dict], enterprise_name: str, target_district: str) -> Tuple[Optional[str], Optional[dict], List[Tuple[int, dict]], str]:
    """
    Select best address from candidates.
    Returns: (best_address, best_candidate, all_scored, status)
    status: "auto_fill" | "candidate" | "not_found"
    """
    if not candidates:
        return None, None, [], "not_found"

    scored = [(score_candidate(c, enterprise_name, target_district), c) for c in candidates]
    scored.sort(key=lambda x: x[0], reverse=True)

    best_score, best = scored[0]

    # Decision logic
    if best_score >= 60:
        # High confidence: auto-fill
        return best["address"], best, scored, "auto_fill"
    elif best_score >= 35 and len(scored) == 1:
        # Only one candidate, moderate confidence
        return best["address"], best, scored, "auto_fill"
    elif best_score >= 35 and len(scored) >= 2 and (best_score - scored[1][0]) >= 20:
        # Best is clearly better than second
        return best["address"], best, scored, "auto_fill"
    elif best_score >= 20:
        # Low confidence but has candidates - need human review
        return None, best, scored, "candidate"
    else:
        # Too low confidence
        return None, best, scored, "not_found"


def generate_report(data_dir: str, auto_filled: list, candidates: list, not_found: list) -> str:
    """Generate a Markdown report for human review."""
    report_path = os.path.join(data_dir, "address_prefill_report.md")

    lines = [
        "# 地址自动填充报告",
        "",
        f"生成日期: {time.strftime('%Y-%m-%d %H:%M')}",
        f"数据来源: 高德地图 POI 搜索",
        "",
        "---",
        "",
        "## 统计摘要",
        "",
        f"| 类别 | 数量 |",
        f"|------|------|",
        f"| 自动填充成功 | {len(auto_filled)} |",
        f"| 候选待审核 | {len(candidates)} |",
        f"| 未找到 | {len(not_found)} |",
        f"| **总计** | **{len(auto_filled) + len(candidates) + len(not_found)}** |",
        "",
        "---",
        "",
    ]

    # Auto-filled section
    lines.extend([
        "## 一、自动填充成功（已写入 YAML）",
        "",
        "以下企业地址已通过 POI 搜索自动填充，建议抽查核实：",
        "",
        "| 序号 | 企业名称 | 所属区 | 填充地址 | 可信度 |",
        "|------|---------|--------|---------|--------|",
    ])
    for i, item in enumerate(auto_filled, 1):
        score = item["best_score"]
        score_label = "高" if score >= 60 else "中"
        lines.append(f"| {i} | {item['name']} | {item['district']} | {item['address']} | {score_label}({score}) |")
    lines.append("")

    # Candidates section
    lines.extend([
        "## 二、候选待审核（需人工选择）",
        "",
        "以下企业 POI 搜索返回多个候选地址，系统无法自动判断。",
        "请在 YAML 文件中手动补充正确地址后重新运行 geocode。",
        "",
    ])
    for item in candidates:
        lines.extend([
            f"### {item['name']}（{item['district']}）",
            "",
            "| 排名 | POI名称 | 地址 | 所在区 | 可信度 |",
            "|------|---------|------|--------|--------|",
        ])
        for rank, (score, c) in enumerate(item["candidates"][:5], 1):
            marker = " **← 推荐**" if rank == 1 else ""
            lines.append(f"| {rank} | {c['name']} | {c['address']} | {c['district']} | {score}{marker} |")
        lines.extend([
            "",
            f"**建议操作**: 从上方候选中选择最准确的地址，或自行查找后更新 `data/杭州/{item['file']}`",
            "",
        ])

    # Not found section
    lines.extend([
        "## 三、未找到（需外部数据源或人工补充）",
        "",
        "以下企业 POI 搜索无结果，建议通过以下渠道补充地址：",
        "- [天眼查](https://www.tianyancha.com) 搜索企业全称",
        "- [高德地图](https://ditu.amap.com/) 手动搜索",
        "- 企业官网 / 环评公示 / 招投标文件",
        "",
        "| 序号 | 企业名称 | 所属区 | 建议查询关键词 |",
        "|------|---------|--------|---------------|",
    ])
    for i, item in enumerate(not_found, 1):
        query = re.sub(r'[（(].*?[）)]', '', item['name']).strip()
        lines.append(f"| {i} | {item['name']} | {item['district']} | `{query}` |")
    lines.append("")

    # Footer
    lines.extend([
        "---",
        "",
        "## 后续步骤",
        "",
        "1. 审核'候选待审核'部分，将正确地址填入对应 YAML 文件",
        "2. 对'未找到'企业，通过天眼查等渠道补充地址",
        "3. 运行 `python geocode.py data/杭州/杭州_XXX_2025.yaml` 重新编码",
        "4. 运行 `python create_map.py data/杭州/杭州_XXX_2025.yaml` 生成新图片",
        "",
    ])

    content = "\n".join(lines)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(content)
    return report_path


def main():
    if not API_KEY:
        print("ERROR: GAODE_API_KEY not set")
        return

    data_dir = "data/杭州"
    yaml_files = sorted([
        f for f in os.listdir(data_dir)
        if f.startswith("杭州_") and f.endswith("_2025.yaml") and "汇总" not in f
    ])

    auto_filled = []
    candidates = []
    not_found = []
    modified_files = set()

    total_processed = 0

    for yf in yaml_files:
        path = os.path.join(data_dir, yf)
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        district = yf.replace("杭州_", "").replace("_2025.yaml", "")

        for e in data.get("enterprises", []):
            # Only process enterprises with empty address AND low-precision coords
            if e.get("address") and e.get("address").strip():
                continue  # Already has address

            # Also skip if already has precise coords
            if e.get("lat") is not None and e.get("lon") is not None:
                if e.get("geocode_level") not in {"区县", "乡镇", "村庄", "未知", "兴趣点", "公交地铁站点", "道路"}:
                    continue  # Already precise

            name = e["name"]
            total_processed += 1
            print(f"\n[{total_processed}] {name} ({district})")

            # Search POI candidates
            poi_candidates = poi_search_candidates(name, CITY, district, API_KEY)

            # Select best
            best_addr, best_candidate, all_scored, status = select_best_address(
                poi_candidates, name, district
            )

            if status == "auto_fill" and best_addr:
                e["address"] = best_addr
                e["_address_source"] = "auto_poi"
                modified_files.add(path)
                auto_filled.append({
                    "name": name,
                    "district": district,
                    "address": best_addr,
                    "best_score": all_scored[0][0],
                    "file": yf,
                })
                print(f"  ✅ Auto-filled: {best_addr} (score: {all_scored[0][0]})")
            elif status == "candidate":
                candidates.append({
                    "name": name,
                    "district": district,
                    "candidates": all_scored[:5],
                    "file": yf,
                })
                print(f"  ⚠️  Candidate: {len(all_scored)} options, best score {all_scored[0][0]}")
                if all_scored:
                    print(f"     Best: {all_scored[0][1]['name']} | {all_scored[0][1]['address']}")
            else:
                not_found.append({
                    "name": name,
                    "district": district,
                    "file": yf,
                })
                print(f"  ❌ Not found")

        # Save modified YAML
        if path in modified_files:
            with open(path, "w", encoding="utf-8") as f:
                yaml.dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
            print(f"  💾 Saved: {yf}")

    # Generate report
    report_path = generate_report(data_dir, auto_filled, candidates, not_found)

    print("\n" + "=" * 60)
    print("地址预填充完成")
    print("=" * 60)
    print(f"自动填充成功: {len(auto_filled)}")
    print(f"候选待审核:   {len(candidates)}")
    print(f"未找到:       {len(not_found)}")
    print(f"\n报告文件: {report_path}")
    print("\n下一步:")
    print("  1. 查看报告，处理候选和未找到的企业")
    print("  2. 运行 geocode.py 重新编码")
    print("  3. 运行 create_map.py 生成图片")


if __name__ == "__main__":
    main()

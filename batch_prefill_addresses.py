#!/usr/bin/env python3
"""Batch prefill enterprise addresses from POI search with human-review reporting.

For enterprises with empty address:
  1. Try POI search with loose name matching to get candidate addresses
  2. Score candidates and auto-fill high-confidence ones
  3. Generate a human-review report for ambiguous / failed cases
"""

import os
import re
import sys
import time
from collections import defaultdict

import yaml

sys.path.insert(0, ".claude/skills/create-pollution-map")
from utils import load_dotenv, get_map_key
from poi_matching import poi_search_candidates, score_candidate, select_best_address

ENV_FILE = ".claude/skills/create-pollution-map/.env"
load_dotenv(ENV_FILE)

API_KEY = get_map_key()
CITY = "杭州"


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

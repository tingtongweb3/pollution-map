#!/usr/bin/env python3
"""Batch prefill enterprise addresses for Fuzhou from POI search.

For enterprises with fuzzy addresses like '福州{name}':
  1. Try POI search with name matching to get candidate addresses
  2. Score candidates and auto-fill high-confidence ones
  3. Generate a report for ambiguous / failed cases
"""

import argparse
import os
import re
import sys
import time

import yaml

sys.path.insert(0, ".claude/skills/create-pollution-map")
from utils import load_dotenv, get_map_key
from poi_matching import poi_search_candidates, select_best_address

ENV_FILE = ".claude/skills/create-pollution-map/.env"
load_dotenv(ENV_FILE)

API_KEY = get_map_key()
CITY = "福州"


def main():
    parser = argparse.ArgumentParser(description="Batch prefill addresses for Fuzhou enterprises")
    parser.add_argument("-c", "--config", required=True, help="Path to the YAML config file")
    parser.add_argument("-o", "--output-report", default=None,
                        help="Path for the prefill report (default: <config_dir>/address_prefill_report.md)")
    args = parser.parse_args()

    if not API_KEY:
        print("ERROR: GAODE_API_KEY not set")
        return

    yaml_path = args.config
    report_path = args.output_report or os.path.join(
        os.path.dirname(yaml_path), "address_prefill_report.md"
    )

    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    auto_filled = []
    candidates = []
    not_found = []
    total_processed = 0

    for e in data.get("enterprises", []):
        name = e["name"]
        addr = e.get("address", "")
        district = e.get("district", "")
        lat = e.get("lat")
        lon = e.get("lon")
        level = e.get("geocode_level", "")

        # Skip if already has a precise address (contains 号/路 and not just "福州{name}")
        if addr and addr != f"福州{name}" and addr != f"福州福州{name}":
            if re.search(r'\d+号|\d+路|\d+街|\d+道', addr):
                continue

        # Skip if already has high-precision coords (POI level with specific address)
        if level not in {"区县", "乡镇", "村庄", "未知", "兴趣点", "公交地铁站点", "道路", "住宅区"}:
            if addr and addr != f"福州{name}" and addr != f"福州福州{name}":
                continue

        total_processed += 1
        print(f"\n[{total_processed}] {name} ({district})")

        poi_candidates = poi_search_candidates(name, CITY, district, API_KEY)
        best_addr, best_candidate, all_scored, status = select_best_address(
            poi_candidates, name, district
        )

        if status == "auto_fill" and best_addr:
            old_addr = e.get("address", "")
            e["address"] = best_addr
            e["_address_source"] = "auto_poi"
            auto_filled.append({
                "name": name,
                "district": district,
                "old_address": old_addr,
                "new_address": best_addr,
                "best_score": all_scored[0][0],
                "candidate": best_candidate,
            })
            print(f"  ✅ Auto-filled: {best_addr} (score: {all_scored[0][0]})")
        elif status == "candidate":
            candidates.append({
                "name": name,
                "district": district,
                "old_address": addr,
                "candidates": all_scored[:5],
            })
            print(f"  ⚠️  Candidate: {len(all_scored)} options, best score {all_scored[0][0]}")
            if all_scored:
                print(f"     Best: {all_scored[0][1]['name']} | {all_scored[0][1]['address']}")
        else:
            not_found.append({
                "name": name,
                "district": district,
                "old_address": addr,
            })
            print(f"  ❌ Not found")

    # Save updated YAML
    with open(yaml_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    print(f"\n💾 Saved: {yaml_path}")

    # Generate report
    report_lines = [
        "# 福州地址自动填充报告",
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
        f"| **总计处理** | **{total_processed}** |",
        "",
        "---",
        "",
        "## 一、自动填充成功",
        "",
        "| 企业名称 | 所属区 | 旧地址 | 新地址 | 可信度 |",
        "|---------|--------|--------|--------|--------|",
    ]
    for item in auto_filled:
        score = item["best_score"]
        score_label = "高" if score >= 60 else "中"
        report_lines.append(
            f"| {item['name']} | {item['district']} | {item['old_address']} | {item['new_address']} | {score_label}({score}) |"
        )
    report_lines.append("")

    if candidates:
        report_lines.extend([
            "## 二、候选待审核",
            "",
        ])
        for item in candidates:
            report_lines.extend([
                f"### {item['name']}（{item['district']}）",
                f"旧地址: {item['old_address']}",
                "",
                "| 排名 | POI名称 | 地址 | 所在区 | 可信度 |",
                "|------|---------|------|--------|--------|",
            ])
            for rank, (score, c) in enumerate(item["candidates"][:5], 1):
                marker = " ← 推荐" if rank == 1 else ""
                report_lines.append(f"| {rank} | {c['name']} | {c['address']} | {c['district']} | {score}{marker} |")
            report_lines.append("")

    if not_found:
        report_lines.extend([
            "## 三、未找到",
            "",
            "| 企业名称 | 所属区 | 建议查询关键词 |",
            "|---------|--------|---------------|",
        ])
        for item in not_found:
            query = re.sub(r'[（(].*?[）)]', '', item['name']).strip()
            report_lines.append(f"| {item['name']} | {item['district']} | `{query}` |")
        report_lines.append("")

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines))

    print("\n" + "=" * 60)
    print("地址预填充完成")
    print("=" * 60)
    print(f"自动填充成功: {len(auto_filled)}")
    print(f"候选待审核:   {len(candidates)}")
    print(f"未找到:       {len(not_found)}")
    print(f"\n报告文件: {report_path}")
    print("\n下一步:")
    print("  1. 审核候选报告")
    print("  2. 运行 geocode.py 重新编码")
    print("  3. 运行 create_map.py 生成图片")


if __name__ == "__main__":
    main()

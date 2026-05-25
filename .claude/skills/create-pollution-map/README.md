# create-pollution-map

Generate high-resolution pollution-source / enterprise distribution maps for **any Chinese city or district** using Gaode Maps as the basemap.

## What it does

- Reads enterprise data from a YAML config file
- Batch-geocodes Chinese addresses via Gaode API (with JSON cache)
- Renders a Gaode/AMap tile basemap via `contextily`
- Draws category-colored scatter markers with numbered labels
- Generates a Chinese legend panel via PIL (avoids matplotlib CJK font issues)
- Combines map + legend into a single high-resolution PNG
- 1km buffer overlays to visualize environmental pressure areas

## Completed Maps

| District | Enterprises | Config | Output |
|----------|-------------|--------|--------|
| 仓山区 | 15家 | `test_cangshan.yaml` | `cangshan_output.png` |
| 鼓楼+台江 | 13家 | `gulou_taijiang.yaml` | `gulou_taijiang_output.png` |
| 晋安区 | 11家 | `jinan.yaml` | `jinan_output.png` |
| 晋安+马尾 | 46家 | `jinan_mawei.yaml` | `jinan_mawei_output.png` |
| 长乐区（重点污染源） | 58家 | `changle.yaml` | `changle_output.png` |
| 长乐区（群众投诉） | 49家 | `changle_complaints.yaml` | `changle_complaints_output.png` |

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set Gaode API key
export GAODE_API_KEY="your_key_here"

# 3. Copy template to city directory and edit
mkdir -p outputs/福州
cp config.yaml outputs/福州/my_project.yaml
# Edit outputs/福州/my_project.yaml: set enterprises, categories, boundary
# 重要：确保每家企业都有非空的 address 字段

# 4. Geocode all addresses (POI名称匹配 + 行政区过滤 + 地址编码交叉验证)
python3 geocode.py -c outputs/福州/my_project.yaml
#    → 若报告失败企业，先补充地址再重新运行

# 5. Audit coordinates (逆编码行政区校验 + POI偏差检查)
python3 audit_coords.py --report -c outputs/福州/my_project.yaml
#    → 若报告"坐标行政区不符"，人工核实实际地址
#    → 确认无误后：python3 audit_coords.py --fix -c outputs/福州/my_project.yaml

# 6. Generate the map (边界强制检查，错误时自动阻止生成)
python3 create_map.py -c outputs/福州/my_project.yaml
#    → 若报告坐标越界，回到步骤5修正
#    → 成功则直接输出准确图片
```

Output: `outputs/<城市>/images/my_project.png` (typically 4000x2700+ px at dpi=200)

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `GAODE_API_KEY` | Gaode Web Service API key | Required; no fallback |

## Prepare Scripts

For new districts, use the prepare scripts as templates:

- `prepare_gulou_taijiang.py` — Geocodes enterprises + fetches boundaries from DataV, generates YAML config
- `prepare_jinan.py` — Single-district version
- `prepare_mawei.py` — Combined multi-district version (loads existing Jin'an data + adds Mawei)
- `prepare_changle.py` — Large-scale district (58 enterprises, name-based geocoding)
- `prepare_changle_complaints.py` — Complaint enterprises from government petition data (49 enterprises)

These scripts use shared utilities from `utils.py`.

## Other Tools

- `audit_coords.py` — Audit enterprise coordinates against Gaode POI. `--report` for inspection, `--fix` for auto-correction
- `verify_status.py` — Verify enterprise operating status via Gaode POI search API
- `geocode.py` — Batch geocoding with dual validation (POI search + address cross-check)

## Config File Reference

```yaml
meta:
  title: "xx市xx区2025年重点污染源单位地理分布图"
  subtitle: "数据来源：xx局 | 共N家 | 坐标：高德地图"
  output_path: "./output.png"
  dpi: 200

map:
  figsize: [18, 14]      # Canvas size in inches
  padding: 500           # Map boundary padding in meters (Web Mercator)
  zoom: 13               # Gaode tile zoom level (11-15 typical)
  basemap_alpha: 0.95    # Basemap opacity

gaode:
  key: ""                # Leave empty; use GAODE_API_KEY env var
  cache_file: "./geocode_cache.json"   # Auto-saved / auto-loaded
  rate_limit: 0.15                     # Seconds between API calls

risk_zones:
  enabled: true
  radius_meters: 1000
  fill_color: "#FF4444"
  fill_alpha: 0.12
  edge_color: "#CC0000"
  edge_width: 0.8
  edge_alpha: 0.25

boundary:
  # GCJ-02 coords [(lon, lat), ...]. Leave empty [] to auto-compute from enterprises.
  coords: []

categories:
  水环境重点排污:
    display: "水环境重点排污"
    color: "#CC0000"
    marker: "o"           # 可选：圆点 ^三角 s方块 D菱形 *星形
  大气环境重点排污:
    display: "大气环境重点排污"
    color: "#FF2200"
    marker: "o"
  土壤污染重点监管:
    display: "土壤污染重点监管"
    color: "#FF6600"
    marker: "o"
  噪声污染投诉:
    display: "噪声污染投诉点"
    color: "#0066CC"
    marker: "^"
  辐射安全许可证:
    display: "辐射安全许可证"
    color: "#9933CC"
    marker: "*"

enterprises:
  - name: "企业A"
    address: "xx市xx区xxx路xx号"
    category: "水环境重点排污"
    label: "企业A\n地址详情"
    data_source: "数据来源名称"
    source_type: "official"   # 可选：official/license/complaint/exposure/eia
    data_date: "2025-03"
    # lat / lon: optional; if absent, geocode.py fills them via API
    # actual_address_verified: true  # 标记为已人工核实实际经营地址（非注册地址）
```

## File Structure

```
create-pollution-map/
├── utils.py                  # Shared utilities (geocode, boundary, font, YAML writer)
├── create_map.py             # Main map rendering脚本
├── geocode.py                # Batch geocoding with POI-first dual validation
├── audit_coords.py           # Coordinate audit against Gaode POI (report / auto-fix)
├── verify_status.py          # Enterprise status verification via Gaode POI
├── config.yaml               # Template config
├── requirements.txt          # Python dependencies
├── skill.yaml                # Claude Code skill definition
└── outputs/                  # 按城市分目录，所有该城市的文件集中存放
    ├── 福州/
    │   ├── *.yaml            # 城市配置文件
    │   ├── prepare_*.py      # 该城市的数据准备脚本
    │   ├── geocode_cache_*.json   # 地理编码缓存
    │   ├── *_boundary.json   # 行政区边界数据
    │   └── images/           # 生成的地图图片
    │       └── *_output.png
    └── 厦门/
        ├── *.yaml
        ├── geocode_cache_*.json
        └── images/
            └── *_output.png
```

**自动路由规则**：
- 相对路径的 `output_path` → 自动路由到 `outputs/<gaode.city>/images/`
- 相对路径的 `cache_file` → 自动路由到 `outputs/<gaode.city>/`
- 绝对路径保持不变，可覆盖自动路由
- 方便打包：`zip -r fuzhou_maps.zip outputs/福州/images/`

## Key Features

### 1. Geocoding Cache
`geocode.py` saves every successful API response to JSON. Re-runs skip cached addresses. Use `--force` to re-geocode.

### 2. Auto Boundary
If `boundary.coords` is empty, computes bounding rectangle from enterprise coordinates with configurable padding. Or fetch real boundaries from DataV GeoJSON by adcode.

### 3. Legend Auto-Sizing
Legend height adapts to enterprise count — no manual adjustment needed.

### 4. Environmental Pressure Zones
1km circular buffers around each enterprise highlight areas where multiple pollution sources overlap, indicating higher environmental pressure.

### 5. Coordinate Validation
Three-layer validation: (1) `geocode.py` uses name-based POI search first, falling back to address geocoding with cross-check; (2) `audit_coords.py` audits all coordinates against Gaode POI before map generation; (3) `create_map.py` checks for outliers (>3 std dev from centroid), duplicates, and coordinates outside Fujian province.

### 6. Fallback Basemap
If Gaode tiles fail, falls back to OpenStreetMap Mapnik automatically.

### 7. Multi-Category Marker Shapes
Different categories use different marker shapes on the map (circle, triangle, square, diamond, star), configured via the optional `marker` field in `categories`. This makes it easy to visually distinguish official监管 data from complaint data, license data, etc.

### 8. Multi-Source Data Fusion
Enterprises can be tagged with `source_type` (`official`, `complaint`, `license`, `exposure`, `eia`). The legend shows a breakdown by source type, helping readers assess data authority.

## 分类体系与标记形状

`categories` 完全自由配置，不限于固定的 4 类。通过 `marker` 字段，不同分类在地图上以不同形状标记，便于一眼区分数据性质：

```yaml
categories:
  水环境重点排污:
    display: "水环境重点排污"
    color: "#CC0000"
    marker: "o"        # 圆点 — 官方监管名录
  噪声污染投诉:
    display: "噪声污染投诉点"
    color: "#0066CC"
    marker: "^"        # 三角 — 群众投诉
  辐射安全许可证:
    display: "辐射安全许可证"
    color: "#9933CC"
    marker: "*"        # 星形 — 许可证信息
  环保违规曝光:
    display: "环保违规被曝光"
    color: "#990033"
    marker: "D"        # 菱形 — 违规曝光
```

**支持的 marker**：

| marker | 形状 | 建议用途 |
|--------|------|---------|
| `o` | 圆点 | 官方监管名录（默认） |
| `^` | 三角形 | 群众投诉/信访 |
| `s` | 方块 | 许可证/资质信息 |
| `D` | 菱形 | 违规曝光/执法案例 |
| `*` | 星形 | 环评批复/特殊类别 |

未配置 `marker` 时默认使用圆点，不影响现有配置。

## 多城市数据准备指南

其他城市没有福州市这样的"重点监管名录"时，可从以下渠道收集数据：

### 一线城市 / 省会城市

| 数据源 | 获取方式 | 适用污染类型 | 权威性 |
|--------|---------|-------------|--------|
| 生态环境局官网-重点监管名录 | 官网下载 / 信息公开申请 | 水、大气、土壤 | 高 |
| 全国排污许可证管理信息平台 | 平台查询 | 水、大气 | 高 |
| 12345 / 信访投诉公示 | 政务服务平台 / 信访局网站 | 噪声、异味、光污染 | 中 |
| 建设项目环评公示平台 | 平台查询 | 各类新建污染源 | 高 |

### 二三线城市

| 数据源 | 获取方式 | 适用污染类型 | 权威性 |
|--------|---------|-------------|--------|
| 省生态环境厅官网 | 省级监管名单 | 水、大气、土壤 | 高 |
| 市级生态环境局执法案例 | 官网公示 | 各类 | 高 |
| 地方城管局-噪声投诉 | 投诉统计 / 公示 | 噪声、光污染 | 中 |

### 县级

| 数据源 | 获取方式 | 适用污染类型 | 权威性 |
|--------|---------|-------------|--------|
| 县级政府信息公开 | 监管名单 / 执法信息 | 各类 | 高 |
| 市级生态环境局分站点 | 执法案例 | 各类 | 高 |
| 实地走访 + 卫星图定位 | 人工核实 | 各类 | 中 |

### 企业 YAML 中的 source_type 标记

收集到多源数据后，在 YAML 中标记来源类型，图例会自动分组统计：

```yaml
enterprises:
  - name: "某餐饮企业"
    category: "噪声污染投诉"
    data_source: "福州市12345平台2026年投诉公示"
    source_type: "complaint"    # official / license / complaint / exposure / eia
```

## 设计原则：一次生成准确图片

本工具链的设计目标是：**从原始数据到最终图片，一次执行完成，无需反复修正**。

为此采用以下策略：

1. **错误前置拦截**：所有可能导致坐标错误的环节（POI名称匹配、行政区过滤、边界检查）都在生成地图前执行，发现问题即阻止流程继续
2. **从不静默通过**：任何坐标异常（偏差>500m、行政区不符、边界外）都会显式报告并建议修正方式，不会生成"可能有问题的地图"
3. **地址必填**：`config.yaml` 中的 `address` 字段不允许为空，确保交叉验证机制始终可用
4. **验证即修正**：`audit_coords.py --fix` 自动修正可确认的错误（POI偏差<500m且名称匹配），减少人工工作量

## 坐标准确性保障

为避免地图坐标与实际地址不符，本 skill 采用三层验证机制：

### 第一层：geocode.py — 双重验证地理编码

地理编码时不再仅凭地址字符串，而是：

1. **名称POI搜索优先** — 用企业全称搜索高德POI，获取注册坐标（精度通常为门址/兴趣点级）
   - **POI名称匹配**：返回的POI名称必须与企业名称匹配（去除行政区前缀和公司后缀后进行相似度比较），拦截同名/近似名干扰
   - **行政区过滤**：从企业名称提取区县关键词（如"闽侯县"），POI地址必须包含该关键词，拦截跨区/跨市匹配
2. **地址编码兜底** — POI未找到时，用地址字符串编码
   - **address为空时**：若POI搜索失败，直接标记为编码失败，提示补充地址，不再尝试无验证的编码
3. **交叉验证** — 两者都成功时，偏差 >1km 告警，>5km 强制采用POI坐标并提示地址可能错误
4. **模糊地址拒绝** — 仅返回"乡镇/村庄"级别的地址标记为精度不足，需人工补充到门牌号

### 第二层：audit_coords.py — 生成前坐标审计

```bash
python3 audit_coords.py --report -c config.yaml   # 仅查看报告
python3 audit_coords.py --fix -c config.yaml      # 自动修正偏差>500m的企业
```

审计内容：
- 每家企业坐标 vs 高德POI坐标偏差
- **坐标行政区反向校验**：逆地理编码获取坐标实际所在区县，与企业名称中的期望区县对比，不一致则标记"坐标行政区不符"
- 坐标是否超出福建省范围
- 坐标是否异常偏离中心点（>3倍标准差且>15km）
- 地理编码精度等级（乡镇/村庄级标记为警告）

### 第三层：create_map.py — 生成时合理性检查

自动生成地图前检查：
- 是否有坐标在福建省外
- 是否有企业坐标完全相同（地址模糊导致）
- 是否有异常离群点（距中心>15km）
- **行政区边界强制检查**：若 `boundary.coords` 已配置，所有企业坐标必须落在边界多边形内；越界则阻止地图生成
- 发现任何严重错误时**自动中止生成**，避免产出错误地图

### 第四层：注册地址 vs 实际污染源地址

**问题根源**：官方名录通常登记的是企业**工商注册地址**，而重点污染源单位名单需要的是**实际产生污染的设施位置**（养殖场、排污口、生产车间、锅炉房）。两者可能相距几十公里。

**典型案例**：
- 健诚农牧：注册地址在福州市区新店镇，实际养殖场在长乐区文岭镇石壁村（相距43km）
- 卓能科技：注册地址在福州市区北二环东路，实际热源工厂在长乐区松下镇首祉村（相距50km）

**检测方法**：

1. **行政区一致性检查**（`audit_coords.py` 自动执行）
   - 如果企业名称含"长乐区"，但地址编码到"晋安区" → 标记为"注册地址与行政区不符"
   - 提示用户通过网络搜索（企业官网、环评公示、政府公告）核实实际经营地址

2. **`actual_address_verified` 标记**
   - 当通过网络检索确认实际地址后，在 YAML 中标记：
     ```yaml
     enterprises:
       - name: 福州市长乐区健诚农牧综合开发有限公司
         address: 福州市长乐区文岭镇石壁村
         actual_address_verified: true
     ```
   - 标记后，`audit_coords.py` 会跳过该企业的 POI 偏差检查（避免注册地址POI与实际地址的误报）
   - `create_map.py` 会在报告中显示"N家已人工核实为实际经营地址"

### 常见坐标错误及避免方法

| 错误类型 | 案例 | 避免方法 |
|----------|------|----------|
| 名录地址错误 | 省立医院"金洲路17号"→实际"金榕南路516号" | POI搜索发现偏差1.7km，自动修正 |
| 模糊地址编码为乡镇中心 | 10家企业都用"金峰镇"→全部重叠 | 模糊地址标记为精度不足，需补充门牌号 |
| 名称编码错误 | 卓能科技编码到50km外市区 | POI搜索返回正确坐标，交叉验证发现偏差 |
| 企业搬迁未更新 | 金山污水处理厂实际在联建村 | 网络检索核实实际地址后修正 |
| **注册地址≠实际地址** | 健诚农牧注册在市区，养殖场在文岭镇 | `audit_coords.py` 行政区一致性检查 + 网络检索 |

## Coordinate Systems

| System | Used By | Notes |
|--------|---------|-------|
| **GCJ-02** | Gaode API / Gaode tiles / Boundary config | Native; no conversion needed |
| **EPSG:3857** | Matplotlib / contextily | Web Mercator; used for all plotting |

## Tips

- **Gaode Key**: Must be a "Web Service" key, not "Web JS API". Get one free at [lbs.amap.com](https://lbs.amap.com).
- **Zoom level**: Start with 13 for a district. Use 11 for a whole city, 15 for an industrial park.
- **Font**: PIL tries PingFang -> Heiti -> Arial Unicode -> Noto CJK. On Linux, install `fonts-noto-cjk`.

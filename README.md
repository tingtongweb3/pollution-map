# 污染源分布图生成器

**中文 | [English](README.en.md)**

基于高德地图 API 的企业污染源地理分布可视化工具。从生态环境局公开数据中提取重点监管企业名录，自动地理编码并生成带图例的高分辨率分布图。

## 功能

- **数据提取**：从政府公开 PDF/HTML 页面自动提取企业名称、地址、污染类别
- **地理编码**：使用高德地图 API 将地址转换为经纬度坐标，带本地缓存
- **风险评分**：根据污染类别、环评等级、行政处罚记录计算低/中/高风险等级
- **地图渲染**：支持按污染类别或风险等级两种渲染模式
- **批量处理**：支持多城市、多区县批量生成

## 环境要求

- Python >= 3.9
- 高德地图 Web服务 Key（[申请](https://lbs.amap.com/api/webservice/guide/create-project/get-key)）

## 安装

```bash
# 克隆仓库
git clone <repo-url>
cd map2image

# 安装依赖
pip install -r .claude/skills/create-pollution-map/requirements.txt

# 配置 API Key
export GAODE_API_KEY="你的高德地图Web服务Key"
```

依赖清单：`matplotlib`, `numpy`, `contextily`, `geopandas`, `shapely`, `Pillow`, `PyYAML`, `requests`, `beautifulsoup4`, `pypdf`

## 快速开始

### 1. 从现有 YAML 生成地图

```bash
python3 .claude/skills/create-pollution-map/create_map.py \
    -c data/福州/福州_主城区_2026.yaml
```

输出图片：`data/福州/images/福州_主城区_2026_output.png`

### 2. 完整流水线（发现数据 → 提取 → 编码 → 制图）

```bash
# 从政府公示页面提取企业数据并生成地图
python3 .claude/skills/create-pollution-map/auto_pipeline.py \
    --city 福州 --district 仓山区 --year 2026 \
    --urls official:https://www.fuzhou.gov.cn/xxx.pdf \
    --risk-assessment \
    --auto-confirm
```

### 3. 批量填充模糊地址

对于地址不精确的企业，使用高德 POI 搜索自动补全：

```bash
python3 batch_prefill_fuzhou.py
```

## 项目结构

```
.claude/skills/create-pollution-map/   # 核心代码
  create_map.py          # 地图渲染（主入口）
  geocode.py             # 地理编码与缓存
  collect_data.py        # 从 PDF/HTML 提取企业数据
  risk_scoring.py        # 环境风险评分引擎
  crawl_fuzhou_penalties.py  # 行政处罚记录爬虫
  auto_pipeline.py       # 提取 → YAML → 编码 → 制图 流水线
  utils.py               # 通用工具函数

# 根目录辅助脚本
batch_prefill_fuzhou.py       # 福州地址 POI 补全
batch_prefill_addresses.py    # 杭州地址 POI 补全
generate_fuzhou_districts.py  # 生成福州各区 YAML

data/                    # 数据目录（按城市）
  福州/
    福州_主城区_2026.yaml    # 企业配置（名称/地址/类别/坐标）
    images/                  # 生成图片（.gitignore）
```

## 配置 YAML 格式

每个城市/区县的配置是一个 YAML 文件，包含：

```yaml
meta:
  title: 福州主城区2026年重点污染源单位地理分布图
  subtitle: 数据来源：福州生态环境局...
map:
  figsize: [18, 14]
  zoom: 12
gaode:
  key: ''        # 留空则使用环境变量 GAODE_API_KEY
  cache_file: ./data/福州/geocode_cache_福州.json
categories:
  水环境:
    display: 水环境重点排污
    color: '#CC0000'
    emoji: 🌊
enterprises:
  - name: 福州市中医院
    address: 鼓东路102号
    categories: [水环境]
    district: 鼓楼
    lat: 26.089877
    lon: 119.302288
```

## 数据来源

- 各城市生态环境局年度环境监管重点单位名录
- 环境违法曝光台（行政处罚记录）

数据来源于政府公开信息，仅供环境研究和公众监督参考。

## License

MIT

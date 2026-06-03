# 污染源分布图生成器

基于高德地图 API 的企业污染源地理分布可视化工具。从生态环境局公开数据中提取重点监管企业名录，自动地理编码并生成带图例的高分辨率分布图。

## 功能

- **数据提取**：从政府公开 PDF/HTML 页面自动提取企业名称、地址、污染类别
- **地理编码**：使用高德地图 API 将地址转换为经纬度坐标，带本地缓存
- **风险评分**：根据污染类别、环评等级、行政处罚记录计算低/中/高风险等级
- **地图渲染**：支持按污染类别或风险等级两种渲染模式
- **批量处理**：支持多城市、多区县批量生成

## 快速开始

### 安装依赖

```bash
pip install -r .claude/skills/create-pollution-map/requirements.txt
```

### 配置 API Key

```bash
# 创建 .env 文件
export GAODE_API_KEY="你的高德地图Web服务Key"
```

### 生成单张地图

```bash
python3 .claude/skills/create-pollution-map/create_map.py \
    -c data/福州/福州_主城区_2026.yaml
```

### 完整流水线（提取 → 编码 → 制图）

```bash
python3 .claude/skills/create-pollution-map/auto_pipeline.py \
    --city 福州 --district 仓山区 --year 2026 \
    --urls official:https://... \
    --risk-assessment
```

## 项目结构

```
.claude/skills/create-pollution-map/   # 核心代码
  create_map.py          # 地图渲染
  geocode.py             # 地理编码
  collect_data.py        # 数据提取
  risk_scoring.py        # 风险评分引擎
  crawl_fuzhou_penalties.py  # 行政处罚爬虫
  auto_pipeline.py       # 自动流水线

data/                    # 数据目录（按城市）
  福州/
    福州_主城区_2026.yaml    # 企业配置
    images/                  # 生成图片（gitignore）
```

## 数据来源

- 各城市生态环境局年度环境监管重点单位名录
- 环境违法曝光台（行政处罚记录）

## License

MIT

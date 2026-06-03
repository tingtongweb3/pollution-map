# Pollution Source Distribution Map Generator

**[中文](README.md) | English**

A visualization tool for enterprise pollution source geographic distribution based on the Gaode Maps API. Extracts key regulated enterprise lists from government public data, performs automatic geocoding, and generates high-resolution distribution maps with legends.

**[中文](README.md) | English**

## Features

- **Data Extraction**: Automatically extract enterprise names, addresses, and pollution categories from government PDF/HTML pages
- **Geocoding**: Convert addresses to latitude/longitude coordinates using Gaode Maps API with local caching
- **Risk Scoring**: Calculate low/medium/high risk levels based on pollution category, EIA level, and administrative penalty records
- **Map Rendering**: Support for two rendering modes — by pollution category or by risk level
- **Batch Processing**: Support batch generation for multiple cities and districts

## Requirements

- Python >= 3.9
- Gaode Maps Web Service Key ([Apply here](https://lbs.amap.com/api/webservice/guide/create-project/get-key))

## Installation

```bash
# Clone the repository
git clone <repo-url>
cd pollution-map

# Install dependencies
pip install -r .claude/skills/create-pollution-map/requirements.txt

# Configure API Key
export GAODE_API_KEY="Your Gaode Maps Web Service Key"
```

Dependency list: `matplotlib`, `numpy`, `contextily`, `geopandas`, `shapely`, `Pillow`, `PyYAML`, `requests`, `beautifulsoup4`, `pypdf`

## Quick Start

### 1. Generate map from existing YAML

```bash
python3 .claude/skills/create-pollution-map/create_map.py \
    -c data/Fuzhou/Fuzhou_MainDistrict_2026.yaml
```

Output image: `data/Fuzhou/images/Fuzhou_MainDistrict_2026_output.png`

### 2. Full pipeline (discover data → extract → geocode → render)

```bash
# Extract enterprise data from government disclosure page and generate map
python3 .claude/skills/create-pollution-map/auto_pipeline.py \
    --city Fuzhou --district Cangshan --year 2026 \
    --urls official:https://www.fuzhou.gov.cn/xxx.pdf \
    --risk-assessment \
    --auto-confirm
```

### 3. Batch fill fuzzy addresses

For enterprises with imprecise addresses, use Gaode POI search to auto-complete:

```bash
python3 batch_prefill_fuzhou.py
```

## Project Structure

```
.claude/skills/create-pollution-map/   # Core code
  create_map.py          # Map rendering (main entry)
  geocode.py             # Geocoding and caching
  collect_data.py        # Extract enterprise data from PDF/HTML
  risk_scoring.py        # Environmental risk scoring engine
  crawl_fuzhou_penalties.py  # Administrative penalty crawler
  auto_pipeline.py       # Extract → YAML → Geocode → Render pipeline
  utils.py               # Common utilities

# Root-level helper scripts
batch_prefill_fuzhou.py       # Fuzhou address POI completion
batch_prefill_addresses.py    # Hangzhou address POI completion
generate_fuzhou_districts.py  # Generate Fuzhou district YAMLs

data/                    # Data directory (by city)
  Fuzhou/
    Fuzhou_MainDistrict_2026.yaml  # Enterprise config (name/address/category/coords)
    images/                        # Generated images (.gitignore)
```

## YAML Config Format

Each city/district config is a YAML file containing:

```yaml
meta:
  title: Fuzhou Main District 2026 Key Pollution Source Distribution Map
  subtitle: Data source: Fuzhou Ecology Environment Bureau...
map:
  figsize: [18, 14]
  zoom: 12
gaode:
  key: ''        # Leave empty to use GAODE_API_KEY env var
  cache_file: ./data/Fuzhou/geocode_cache_Fuzhou.json
categories:
  Water:
    display: Key Water Pollution Discharge
    color: '#CC0000'
    emoji: 🌊
enterprises:
  - name: Fuzhou Traditional Chinese Medicine Hospital
    address: 102 Gudong Road
    categories: [Water]
    district: Gulou
    lat: 26.089877
    lon: 119.302288
```

## Data Sources

- Annual Key Regulated Enterprise Lists from city ecology environment bureaus
- Environmental Violation Exposure Platform (administrative penalty records)

Data is sourced from government public information for environmental research and public supervision purposes only.

## License

MIT

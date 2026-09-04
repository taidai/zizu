# ZiZu

> Self-hosted IoT Platform · Open-source Industrial IoT Low-code Platform
>
> **Deliver industrial control systems with simple configuration** — A lightweight alternative to ThingsBoard.
>
> Current field baseline: **v0.8.3**. See the bilingual [ZiZu Technical Architecture](docs/ZIZU-TECHNICAL-ARCHITECTURE.md) for the current product and runtime model. Where older sections of this README disagree, the core architecture specification, latest accepted ADRs, and current source code take precedence.

[中文](README.md) | **English** | [官网 www.holoems.com](https://www.holoems.com)

---

## What is ZiZu

ZiZu is an IoT platform designed for **solar-storage-charging EMS, industrial energy monitoring, and remote device control** scenarios. The core philosophy is "**Configuration as Platform**" — all business logic is generated through UI configuration, with no hard-coded logic.

It integrates the four pillars of industrial IoT (device access / data pipeline / tag computation / control rules) into a pluggable pipeline:

```
Neuron ──MQTT──► nanoMQ ──► [F0 Data Pipeline] ──► [TimescaleDB]
                              │
                    ┌─────────┼─────────┐
                    ▼         ▼         ▼
                  [F1 Hook] [F3 Hook] [F2 Hook]
                  Tag Calc   Node Agg   Control
```

> **The pipeline is the skeleton; feature domains are organs attached to it.**

---

## Feature Domains

ZiZu capabilities are divided into four feature domains with **progressive delivery**:

| Domain | One-liner | Core Capability | User Value |
|--------|-----------|-----------------|------------|
| **F0** | Data pipeline stream processing | MQTT → Parse → Normalize → Hook Chain → TSDB | **See raw data as soon as devices come online** |
| **F1** | Custom physical/virtual tags | PhysicalTag collection mapping + LogicalTag (SymPy formula evaluation) | **Flexibly define any derived metrics** |
| **F3** | Node tree tag mounting + aggregation | 5-layer unified tree + per-layer SUM/AVG/MAX aggregation | **Every node is a first-class citizen with independent real-time values** |
| **F2** | Control strategy (GoRules) | RPC write-back + JDM decision tables + audit logs | **Safely and controllably reverse-control devices** |

### 5-Layer Node Tree

```
Site
 └── Station
      └── EnergyNode
           ├── ESS  Energy Storage  (PCS / BMS / Meter)
           ├── PV   Photovoltaic   (Inverter / PV Meter)
           ├── GRID Grid Connection (Grid Meter)
           └── EVSE Charging Pile   (Charger / Charging Meter)
                └── Tag
                     ├── PhysicalTag  ← Neuron collection
                     └── LogicalTag   ← Formula computation
```

A single JSON describes the complete hierarchy — no split into multiple concepts (TB's Asset / Device / Profile triple is merged into one Node Tree).

---

## Tech Stack

| Layer | Technology | Description |
|-------|-----------|-------------|
| **Device Access** | [Neuron](https://github.com/emqx/neuron) | Industrial protocol gateway (Modbus / OPC-UA / IEC104 ...) |
| **Message Bus** | [nanoMQ](https://github.com/nanomq/nanomq) | Lightweight MQTT 5.0 Broker |
| **Backend** | [FastAPI](https://fastapi.tiangolo.com/) + Python 3.12 | Data pipeline + Hook chain + REST API |
| **Time-series Storage** | [TimescaleDB](https://www.timescale.com/) | PostgreSQL + Hypertable + Continuous Aggregates (CAGG) |
| **Rule Engine** | [GoRules ZEN](https://github.com/gorules/zen) | JDM decision tables / graphs (F2 control domain) |
| **Frontend** | React + Vite + TypeScript + Tailwind | Neumorphism style |
| **Unit Conversion** | [pint](https://github.com/hgrecco/pint) | Normalizer (raw value → engineering value) |
| **Formula Evaluation** | [SymPy](https://github.com/sympy/sympy) | Virtual tag symbolic computation |

---

## Current Progress

| Module | Status | Description |
|--------|--------|-------------|
| **F0 Data Pipeline** | ✅ Delivered | MQTT→Parser→Normalizer→TSDB full pipeline, ~10 msg/s sustained ingestion |
| **F0 Visualization V1** | ✅ Delivered | Tag list + inline offset/scale editing + dual real-time values (raw/eng) + WebSocket push |
| **Neuron Tag Sync** | ✅ Delivered | `sync_neuron_tags.py` one-click discovery of nodes/groups/tags, auto-ingestion |
| **F0 Snapshot Blackboard** | ✅ Delivered | Node-level full JSONB snapshots with timestamp alignment |
| **F1 Virtual Tags** | 🔨 Planning | SymPy formula engine + cascade propagation |
| **F3 Node Aggregation** | 🔨 Planning | 5-layer tree + per-layer aggregated values |
| **F2 Control Rules** | 🔨 Planning | GoRules JDM + RPC write-back + audit logs |

---

## Quick Start

### Prerequisites

- Docker + Docker Compose
- Python 3.12+ (local development)
- Node.js 18+ (frontend build)

### 1. Clone & Configure

```bash
git clone https://github.com/taidai/zizu.git
cd zizu
cp .env.example .env   # Modify database password as needed
```

### 2. One-Click Start (Recommended)

```bash
docker compose up -d --build
```

Access:
- `http://localhost:9000` — Frontend (tag management + real-time trends)
- `http://localhost:9000/api/docs` — Swagger API documentation

> First startup automatically executes `init-db/*.sql` to initialize the database.

### 3. Local Development (Optional)

**Backend**:
```bash
cd backend
pip install fastapi "uvicorn[standard]" psycopg2-binary paho-mqtt loguru pydantic pydantic-settings pint websockets
uvicorn app.main:app --reload --port 9000
```

**Frontend**:
```bash
cd frontend
npm install
npm run dev    # Vite dev server @5173
```

### 4. e606 Trimmed Kernel Deployment

```bash
docker compose -f docker-compose.yml -f docker-compose.e606.yml up -d
```

> e606 uses `network_mode: host` + `tmpfs: /dev/mqueue`, ports directly occupy the host.

---

## Project Structure

```
zizu/
├── backend/
│   ├── app/
│   │   ├── api/            # REST + WebSocket routes
│   │   │   ├── health.py   #   Pipeline health status
│   │   │   ├── nodes.py    #   Node list
│   │   │   ├── tags.py     #   Tag CRUD + inline editing
│   │   │   ├── telemetry.py#   Raw telemetry query
│   │   │   ├── snapshots.py#   Node snapshot query
│   │   │   ├── admin.py    #   Developer tools
│   │   │   └── websocket.py#   Real-time value push
│   │   ├── core/           # Configuration (pydantic-settings)
│   │   ├── db/             # Database connection pool
│   │   ├── models/         # Pydantic data models
│   │   ├── services/       # Core pipeline
│   │   │   ├── mqtt_client.py      #  MQTT access layer
│   │   │   ├── parser.py           #  Neuron message parsing
│   │   │   ├── normalizer.py      #  pint unit normalization
│   │   │   ├── pipeline.py        #  Hook chain + batch flush
│   │   │   └── telemetry_store.py #  TSDB write
│   │   └── main.py
│   ├── scripts/
│   │   └── sync_neuron_tags.py    # Neuron API → t_tags auto sync
│   ├── tests/
│   └── pyproject.toml
│
├── frontend/
│   ├── src/
│   │   ├── App.tsx        # Main page (tag table + real-time values + status bar)
│   │   ├── api/           # HTTP/WS client
│   │   ├── components/    # charts / tree / ui
│   │   └── ...
│   └── Dockerfile         # Nginx static service
│
├── init-db/
│   ├── 001-schema.sql     # Tables + Hypertable + CAGG
│   ├── 002-test-data.sql  # Test data
│   ├── 003-real-device-mapping.sql # Real device mapping
│   └── 004-node-snapshot.sql # Node snapshot table
│
├── config/
│   └── nanomq.conf        # nanoMQ configuration
│
├── docs/
│   ├── architecture-v1.md      # Architecture design
│   ├── ui-style-guide.md      # UI style guide
│   └── decisions/             # ADR decision records
│       ├── g11-feature-domains.md   # Feature domain architecture
│       ├── g7-goal-breakdown.md      # Goal breakdown
│       └── ...
│
├── docker-compose.yml        # Three-service orchestration (TimescaleDB + FastAPI + NanoMQ)
├── docker-compose.e606.yml   # e606 trimmed kernel override
└── .env.example             # Environment variable template
```

---

## Core API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/health` | Pipeline status (msg/s, ingestion rate, last message) |
| GET | `/api/v1/nodes` | Node list (with tag count) |
| GET | `/api/v1/tags?node_id=X&page=1` | Paginated tag query (with offset/scale) |
| PUT | `/api/v1/tags/{tag_id}` | Modify scale / offset / unit |
| PUT | `/api/v1/tags/batch` | Batch update scale / offset |
| GET | `/api/v1/telemetry` | Raw telemetry data query |
| GET | `/api/v1/snapshots` | Node snapshot query (data blackboard) |
| POST | `/api/v1/query` | SELECT-only SQL query |
| WS | `/api/v1/ws/telemetry` | Real-time raw/engineering value push |

---

## Data Model (Simplified)

```sql
-- Nodes (5-layer tree)
t_nodes(id, parent_id, node_type, name, ...)

-- Tags (physical + logical unified table)
t_tags(
  id, node_id, name, data_type,
  scale_factor, value_offset,     -- Engineering conversion: eng = (raw + offset) × scale
  unit_from, unit_to,
  source, is_virtual, ...
)

-- Telemetry (TimescaleDB Hypertable)
t_telemetry(ts, node_id, tag_id, value_int, value_float, value_bool, value_str)

-- Node Snapshots (data blackboard)
t_node_snapshot(ts, node_id, data JSONB, raw_data JSONB, raw_message JSONB)

-- Continuous Aggregates (multi-granularity query)
cagg_telemetry_1min, cagg_telemetry_5min, cagg_telemetry_1h
```

**Engineering value conversion formula**:

```
engineering_value = (raw_value + value_offset) × scale_factor
```

Example: BMS current raw value 16500, `value_offset = -16000`, `scale_factor = 0.1`
→ `(16500 + (-16000)) × 0.1 = 50 A`

---

## Neuron Tag Sync

After new devices come online, no manual tag entry is required:

```bash
python backend/scripts/sync_neuron_tags.py \
  --neuron-url http://localhost:7000 \
  --dry-run    # Preview first, remove this flag to execute for real
```

The script will: Login to Neuron → Discover all driver nodes → Enumerate groups → Fetch tags → Upsert into `t_nodes` / `t_tags`.

---

## Deployment Notes (Trimmed Kernel / ARM64)

If the target server kernel is trimmed (common on embedded ARM64 devices), Docker may encounter:

- `CONFIG_POSIX_MQUEUE` missing → Container startup mqueue error
- `CONFIG_VETH` missing → Bridge network failure

**Avoidance rules** (both required, built into `docker-compose.yml`):

```yaml
services:
  every-service:
    network_mode: host       # Avoidance #1: Bypass bridge
    tmpfs:
      - /dev/mqueue          # Avoidance #2: Replace kernel mqueue
```

---

## Design Philosophy

1. **Configuration as Platform** — Business logic generated through UI configuration, no hard-coded logic
2. **One Node Tree** — 5-layer unified model, no split into Asset/Device/Profile
3. **Physical/Logical Unified Addressing** — Frontend doesn't distinguish sources, unified `node_path.tag_name`
4. **Rules Follow Nodes** — Rules bind to any layer, automatically inherited by child nodes
5. **Minimal Closed-Loop First** — First complete "device online → data display → rule trigger → control issuance"

See [`docs/decisions/`](docs/decisions/) for ADR decision records.

---

## Links

- **Official Website**: [www.holoems.com](https://www.holoems.com)
- **GitHub**: [github.com/taidai/zizu](https://github.com/taidai/zizu)
- **Documentation**: [docs/](docs/)

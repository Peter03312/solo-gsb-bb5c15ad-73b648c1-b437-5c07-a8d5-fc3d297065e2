# 数字出版版税结算服务

基于 **Python / FastAPI + SQLite** 的版税结算服务：作品阶梯费率管理、销售入账计提、作者分账、退款冲销、关账批次报表，支持幂等重试与并发安全。

## 快速启动

### Docker（推荐，一键启动并自动初始化数据库）

```bash
docker compose up --build
```

或直接使用 Dockerfile：

```bash
docker build -t royalties .
docker run -d -p 8000:8000 -v royalties-data:/data royalties
```

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

数据库 schema 在服务启动时自动创建（幂等），无需手工初始化。

### 地址与端口

| 地址 | 说明 |
| --- | --- |
| `http://localhost:8000` | 服务入口 |
| `http://localhost:8000/docs` | Swagger 交互式 API 文档 |
| `http://localhost:8000/health` | 健康检查 |

### 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | 容器内 `/data/royalties.db`，本地 `./royalties.db` | SQLite 数据库文件路径 |
| `PORT` | `8000` | 服务监听端口（Docker 启动命令使用） |

Compose 默认将宿主机 `8000` 映射到容器 `8000`，数据持久化在命名卷 `royalties-data`。

## 业务规则

- **作品**：录入递增的阶梯销量门槛（首档门槛必须为 0，严格递增）、非负费率（小数，如 `0.08` 表示 8%）、作者分成比例（百分数，合计必须恰好为 100）。
- **销售计提**：销售按**入账序号**（数据库提交顺序）累计册数，与业务时间无关；跨档时分档计提。每册版税 = 每册净价 × 该档费率，**四舍五入到分**；分档金额 = 每册版税 × 该档册数。
- **作者分账**：对整笔计提金额，各作者按分成比例**先向下取整到分**，剩余分按**作者编号顺序**（纯数字编号按数值，其余按字典序）依次补 1 分。
- **退款**：从原销售**最后一册**起按原每册计提冲回（含分账），**不回退累计销量**；同一销售的累计退款册数不得超过原册数（超出返回 400）。
- **关账**：汇总上次关账之后到本次提交前的全部流水（按入账序号），生成**逐笔明细 + 作者合计**的快照报表，落库后**不可修改**（仅提供只读接口）；关账后补录的旧业务流水自然进入下一批次。
- **付款结算（关账后）**：以作者编号、**当前最新关账批次**编号、付款编号、支付金额（分）提交。应计额 = 截至该批次全部**不可改关账快照**中该作者净额之和；已付额 = 该作者全部已确认付款之和；可付额 = `max(应计额 − 已付额, 0)`。支付金额必须为正且不超过可付额；作者/批次不存在返回 404，批次不是当前最新返回 409。
- **负余额（待抵扣）**：关账后退款若使应计额低于已付额，差额记为作者的待抵扣负余额，**后续关账的正额先抵扣**，旧报表与旧付款均不改写；负余额未清零时可付额为 0，不能付款。
- **幂等**：作品 / 销售 / 退款 / 付款以唯一编号为准——同号同内容重试返回原结果（HTTP 200），同号异内容拒绝（HTTP 409）；付款的同号同内容重试**即使之后又有关账也返回原结算结果**（响应为提交时刻的快照）。关账可携带 `request_id` 作为幂等键。
- **并发**：所有入账、关账与结算写操作在进程内串行化并在单事务中提交（`BEGIN IMMEDIATE`），批次归属由**数据库提交顺序**决定，并发结算不会超付。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/works` | 录入作品（阶梯费率 + 作者分成） |
| GET | `/works/{work_id}` | 查询作品 |
| GET | `/works/{work_id}/stats` | 作品销量汇总（累计/已退/净册数） |
| POST | `/sales` | 登记销售流水，返回分档计提与分账明细 |
| GET | `/sales/{sale_id}` | 查询销售流水 |
| POST | `/refunds` | 登记退款流水，返回冲销明细 |
| GET | `/refunds/{refund_id}` | 查询退款流水 |
| POST | `/closes` | 关账，生成不可改报表（可选 `request_id` 幂等键） |
| GET | `/closes` | 关账批次列表 |
| GET | `/closes/{close_id}` | 查询关账报表 |
| POST | `/payments` | 提交作者付款结算（仅当前最新关账批次） |
| GET | `/payments/{payment_id}` | 查询单笔付款结算 |
| GET | `/authors/{author_id}/settlement` | 作者结算总览：分批应计、已付、负余额、可付、付款记录 |

金额单位：响应中 `*_cents` 字段均为**分**；`net_price` 为元（字符串原样返回，避免浮点误差）。

## 调用示例

完整可运行脚本：`./examples/demo.sh`（需服务已启动）。

```bash
# 1. 录入作品：两档阶梯，两位作者 60/40 分成
curl -X POST http://localhost:8000/works -H 'Content-Type: application/json' -d '{
  "work_id": "BOOK-1",
  "title": "示例作品",
  "tiers": [{"threshold": 0, "rate": 0.08}, {"threshold": 1000, "rate": 0.10}],
  "authors": [{"author_id": "A1", "ratio": 60}, {"author_id": "A2", "ratio": 40}]
}'

# 2. 登记销售 1200 册（跨档计提：1000 册×8% + 200 册×10%）
curl -X POST http://localhost:8000/sales -H 'Content-Type: application/json' -d '{
  "sale_id": "S100", "work_id": "BOOK-1",
  "business_time": "2026-09-01T10:00:00",
  "copies": 1200, "net_price": 33.33
}'
# → total_royalty_cents = 333600（1000×267 + 200×333）
# → splits: A1 200160 分，A2 133440 分

# 3. 退款 250 册（从最后一册起冲回：200 册×333 + 50 册×267 = 79950 分）
curl -X POST http://localhost:8000/refunds -H 'Content-Type: application/json' -d '{
  "refund_id": "RF100", "sale_id": "S100", "copies": 250
}'

# 4. 关账，生成不可改报表（request_id 为幂等键，重试返回原报表）
curl -X POST http://localhost:8000/closes -H 'Content-Type: application/json' -d '{
  "request_id": "CLOSE-2026-09"
}'

# 5. 查看报表与批次列表
curl http://localhost:8000/closes/1
curl http://localhost:8000/closes

# 6. 财务确认作者实际付款（close_id 必须是当前最新批次）
curl -X POST http://localhost:8000/payments -H 'Content-Type: application/json' -d '{
  "payment_id": "PAY-2026-09-A1",
  "author_id": "A1",
  "close_id": 2,
  "amount_cents": 100000
}'

# 7. 同付款编号同内容重试 → 原样返回；按作者查询结算总览
curl -X POST http://localhost:8000/payments -H 'Content-Type: application/json' -d '{
  "payment_id": "PAY-2026-09-A1", "author_id": "A1", "close_id": 2, "amount_cents": 100000
}'
curl http://localhost:8000/authors/A1/settlement
curl http://localhost:8000/payments/PAY-2026-09-A1
```

付款结算响应示例：

```json
{
  "payment_id": "PAY-2026-09-A1",
  "author_id": "A1",
  "close_id": 2,
  "amount_cents": 100000,
  "accrued_cents": 152790,
  "paid_before_cents": 0,
  "payable_cents": 152790,
  "paid_after_cents": 100000,
  "negative_balance_cents": 0,
  "created_at": "2026-10-04T12:00:00+00:00"
}
```

作者结算总览响应（`GET /authors/{author_id}/settlement`）：

```json
{
  "author_id": "A1",
  "latest_close_id": 2,
  "accrued_cents": 152790,
  "paid_cents": 100000,
  "negative_balance_cents": 0,
  "payable_cents": 52790,
  "batches": [
    {"close_id": 1, "batch_net_cents": 152190, "accrued_cents": 152190, "created_at": "..."},
    {"close_id": 2, "batch_net_cents": 600,    "accrued_cents": 152790, "created_at": "..."}
  ],
  "payments": [
    {"payment_id": "PAY-2026-09-A1", "close_id": 2, "amount_cents": 100000,
     "accrued_cents": 152790, "paid_before_cents": 0, "payable_cents": 152790, "created_at": "..."}
  ]
}
```

字段说明：`batches[].batch_net_cents` 为该批次快照中作者净额（退款可为负），`accrued_cents` 为截至该批次的累计应计；顶层 `negative_balance_cents = max(paid − accrued, 0)`，`payable_cents = max(accrued − paid, 0)`。

### 错误码（付款结算）

| 状态码 | 场景 |
| --- | --- |
| 400 | 支付金额超过可付额（含可付额为 0 / 存在待抵扣负余额） |
| 404 | 作者或关账批次不存在 |
| 409 | 批次不是当前最新；或同付款编号异内容重试 |
| 422 | 请求体校验失败（如 `amount_cents ≤ 0`） |

响应示例（销售入账，节选）：

```json
{
  "sale_id": "S100",
  "entry_seq": 1,
  "cum_before": 0,
  "cum_after": 1200,
  "segments": [
    {"tier_index": 0, "rate": "0.08", "copies": 1000, "per_copy_royalty_cents": 267, "amount_cents": 267000},
    {"tier_index": 1, "rate": "0.1",  "copies": 200,  "per_copy_royalty_cents": 333, "amount_cents": 66600}
  ],
  "total_royalty_cents": 333600,
  "splits": [
    {"author_id": "A1", "amount_cents": 200160},
    {"author_id": "A2", "amount_cents": 133440}
  ],
  "close_id": null
}
```

## 测试

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

覆盖：阶梯计提、分账余分补齐、退款跨段冲回与超退拦截、幂等重试与冲突、关账批次归属与报表不可改、付款结算（过期批次拒绝/超付拦截/幂等重放/负余额抵扣）、并发入账、关账与结算。

## 项目结构

```
app/
  main.py     # FastAPI 路由
  ledger.py   # 核心账务逻辑（计提/分账/退款/关账）
  db.py       # SQLite 连接与 schema 初始化
  schemas.py  # 请求模型与校验
tests/test_api.py
examples/demo.sh
Dockerfile / docker-compose.yml
```

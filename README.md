# 差异化评价指标库（后端）

面向年度高校考核的完整后端：按办学类型（技能型 / 地方服务型 / 基础研究型）维护
指标、权重、数据来源与生效区间；新口径多人会签；计算时封存口径快照、分数与缺失
证据；已公布结果只能以**更正版本**处理；审计人员可复算任一年度，并分别看清
**迟到数据、撤销签署、并发发布**各自改变了什么。

## 设计要点

- **事件溯源**：所有状态变更都是只追加事件（`EventStore`，JSONL 落盘），聚合状态
  随时可从事件流完整重放；没有 UPDATE/DELETE。
- **哈希链存证**：每条事件含 `prev_hash` 与内容 SHA-256，任何篡改在加载时即被
  `ChainIntegrityError` 检出；每次封存再对口径、截止时刻、数据快照、分数、缺失
  证据计算规范化清单指纹 `manifest`。
- **确定性复算**：封存的是当时的输入快照与权重；评分是纯函数（缺证据指标在已到
  证据指标间重新归一权重），排序以 `(分数降序, 学校代码升序)` 打破并列，任何人对
  同一快照必然算出同一榜单。
- **只更正、不改写**：已封存发布永远保留；更正产生新版本（`correction_of` 指向前
  版），旧版状态置「已更正」。更正记录同时保存：迟到数据清单、区间内撤销签署清
  单、逐校名次/分数总差异，以及「剔除迟到数据」反事实重算得出的迟到独立贡献。
- **并发控制**：封存/更正在事务锁内完成「查已封存 → 写新版本」；并发的第二个封存
  被拒绝并追加 `PUBLICATION_BLOCKED` 审计事件（携带 `txn`），不产生部分写入。
- **角色边界**：评价管理人员（主数据/口径/会签/封存/更正）、高校填报员（填报）、
  审计人员（只读、复算、变更解释）。

### 口径状态机

```
草案 ──发起会签──▶ 会签 ──有效签署≥法定人数──▶ 生效 ──封存──▶ 封存
                    ▲   │                         │
                    └重新签署                      └─被更正口径替代──▶ 被替代
                    │
              撤销签署（留痕；生效后撤销不回改已封存结果）
```

- 生效区间为半开区间 `[start_year, end_year)`；同类型同时只允许一个生效口径，区间
  重叠拒绝（更正替代走显式 `supersedes`）。
- 口径版本号按「类型 + 年度」递增；历史版本永不消失，历史排名按当时封存版本复算，
  不会因新口径而漂移。

### 事件目录

`SCHOOL_REGISTERED`、`METRIC_REGISTERED`、`CALIBER_CREATED`、`COSIGN_STARTED`、
`CALIBER_SIGNED`、`SIGN_REVOKED`、`CALIBER_PUBLISHED`、`CALIBER_SUPERSEDED`、
`DATA_SUBMITTED`（含 `arrived_after_seal`）、`PUBLICATION_SEALED`、
`PUBLICATION_CORRECTED`、`PUBLICATION_BLOCKED`。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验（既有）。
- `src/indicator_registry/`：后端实现。
  - `models.py`：冻结值对象/实体（指标、口径、生效区间、缺失证据、事件）。
  - `store.py`：哈希链追加日志与清单指纹。
  - `scoring.py`：确定性加权评分与榜单差异。
  - `repository.py`：重放聚合与全部写侧业务规则（会签、封存、迟到、更正、并发）。
  - `audit.py`：读模型、复算、变更归因解释。
  - `services.py`：角色鉴权门面。
  - `api.py`：标准库 HTTP 接口（无第三方依赖）。
- `tools/check_contract.py`：契约摘要检查。
- `tools/demo_scenario.py`：端到端场景演示（可运行规格）。
- `tests/`：契约回归、领域规则、HTTP 与真实线程并发竞态测试。

## 验证

```bash
python3 -m unittest discover -s tests -v          # 14 个测试
python3 -m compileall -q src tools tests
python3 tools/check_contract.py domain/contract.json
python3 tools/demo_scenario.py                    # 生成事件库并打印审计报告
```

## HTTP 接口

启动：`python3 -m indicator_registry.api 127.0.0.1 8080 data/events.jsonl`
（在 `src/` 目录下或设置 `PYTHONPATH=src`）。

请求头：

| 头 | 含义 |
|---|---|
| `X-Actor-Role` | `admin` / `filer` / `auditor`（角色别名，避免非 ASCII 头） |
| `X-Actor-Name` | 操作人（百分号编码），会签签名以该姓名为准 |
| `X-Idempotency-Key` | 写命令幂等键；填报必填，重放返回原记录 |
| `X-Txn` | 并发发布/更正的事务标识，写入拦截留痕 |

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/schools` | 登记高校（含办学类型） |
| POST | `/metrics` | 登记指标（必须含 `source` 数据来源） |
| POST | `/calibers` | 创建口径草案（权重和为 1、区间覆盖年度、`required_signers≥2`） |
| POST | `/calibers/{id}/cosign/start` `/sign` `/revoke` | 发起/签署/撤销会签 |
| POST | `/calibers/{id}/publish` | 会签达标后生效；body `{"supersedes": id}` 用于更正替代 |
| POST | `/submissions` | 填报；截止后到达自动 `迟到`，无证据 `缺失证据` |
| POST | `/calibers/{id}/seal` | 封存发布（并发第二个返回 409 并留痕） |
| POST | `/publications/correct` | 更正版本：`old_publication_id` + `new_caliber_id` + `reason` |
| GET | `/calibers` `/metrics` `/publications/{id}` | 读模型 |
| GET | `/publications?year=&school_type=` | 发布版本历史（旧版保留） |
| GET | `/audit/verify` | 哈希链完整性（仅审计） |
| GET | `/audit/recompute?year=&school_type=` | 逐版重算分数、缺失证据、清单指纹（仅审计） |
| GET | `/audit/changes?year=&school_type=` | 并发拦截 + 每次更正的迟到/撤销/名次变化归因（仅审计） |

## 复算与归因说明

- `recompute` 返回 `scores_match / missing_match / manifest_match`：分别用封存快照
  重跑评分并重建清单指纹，与封存值逐一比对。
- `explain_changes` 对每次更正给出：
  - `overall_ranking_diff`：新旧榜单逐校分数与名次差；
  - `late_data`：迟到提交明细（提交时刻 vs 封存截止时刻），以及在**新口径不变**前提
    下纳入/剔除迟到数据的反事实分数差——迟到数据的独立贡献；
  - `revoked_signatures`：两版之间发生的签署撤销（撤销不回改旧版，只随新版留痕）；
  - `concurrent_publish_blocks`：被拒绝的并发/重复封存请求及其 `txn`。

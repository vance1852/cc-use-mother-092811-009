# 综合交通协同服务：普通公路养护资金决策与执行

本项目在综合交通运输共享基础能力（运营机构、操作者、场所、参考资料登记，角色权限、
请求幂等、SQLite 事务、哈希串联审计）之上，构建了一套**可说明取舍、可全程追踪、
历史不可改写**的县域普通公路养护资金决策与执行服务。

## 业务问题与对策

县域集中养护期内，财政面对的申请既有高流量干线，也有交通量不大但承担乡村唯一通达
功能的路段。沿用按里程平均分配会挤掉灾害风险高、替代路线少的项目。本服务以六维证据
加权评分取代里程均摊：

| 维度 | 证据字段 | 政策取向 |
| --- | --- | --- |
| 设施状况 `condition` | `pci` | 路况越差需求越急 |
| 服务人口 `population` | `served_population` / `sole_access` | 覆盖人口 + 唯一通达奖励 |
| 替代性 `alternatives` | `alternative_routes` | 无替代路线断通即断联 |
| 灾害暴露 `hazard` | `hazard_level` | 暴露越高越优先 |
| 历史维修 `maintenance_history` | `repeated_repair` 等 | 反复维修识别结构性欠账 |
| 交通量 `traffic` | `aadt` | 高流量按比例计入但不主导 |

默认政策 `v2026.1` 中灾害+状况+替代性合计权重 70%，交通量仅 5%——乡村唯一通达、
高灾害路段能够排在高流量干线之前。评分政策**只能换版、不能修改**，每个维度都给出
中文评分理由，形成可以向各方说明的投资组合。

## 核心机制

### 1. 申报截止冻结证据与评分政策

- 轮次开放期内可随时补录多维证据；`freeze` 时把每个申报路段截止时点前的最新各维
  证据**快照固化**（`round_evidence_snapshots`），并固定当时 active 的政策版本。
- 截止日后补录的证据不影响已冻结评分（有测试覆盖）。
- 冻结后按六维加权打分、排名；被其他项目依赖的前置项目给予小额排序加分。

### 2. 批准前查重与互斥识别

在批准（`decide`）之前全量识别四类问题，发现任一冲突即拒绝形成组合：

- **重复申报 / 同一路段拆项**：同一路线桩号区间重叠（申报时即时拦截 + 批准前全量复核，
  并跨轮次比对历史已批准项目）；
- **互斥施工窗口**：桩号重叠、或同一路段、或存在依赖关系的项目，施工日期窗口不得重叠；
- **依赖顺序**：后续项目开工不得早于前置项目完工；项目依赖不允许成环。
- 冲突可通过 `GET /funding/conflicts` 提前查看；整改（如撤项）后才能决策。

### 3. 各级资金约束与瀑布式分配

- 每个轮次分别登记中央/省/县三级资金信封（另有紧急限额信封）。
- 按排名依次安排，**县级先兜底、不足逐级上求**；任一层都无法全额覆盖的项目进入
  候补名单（waitlisted），不做超额承诺。
- 批准即产生 `commit` 台账并把每个里程碑置为 `reserved`（额度承诺）。

### 4. 里程碑承诺、支付与结算

- 申请金额必须拆分为金额之和相等的里程碑；批准后里程碑进入承诺保留状态。
- 支付按各级资金承诺占比分摊到对应信封；里程碑累计支付不得超过其承诺额。
- 全部里程碑付清后项目自动 `settled`；支付不可冲销（冲正走结余/违规追回）。

### 5. 变更、取消、结余回收与关账不可逆

- **会计期间**：台账分录必须落在唯一开放期间；期间关账后，承诺、支付、变更、取消、
  追回一律拒绝写入——变更/取消/回收**不能改写已关账期间**，只能在新期间登记。
- 变更：追加需各级余额可覆盖；缩减释放未付承诺，但不得低于已付金额。
- 取消：释放未付承诺；已付部分需要时走追回。
- 追回：仅对已结算项目，累计追回不超过已支付净额。

### 6. 紧急抢修限额例外 + 独立复核

- 轮次冻结/定稿后仍可提交 `emergency` 申请（限额信封独立，不占常规盘子）。
- 必须经**申报人之外、且不属于申报单位**的独立复核员（reviewer/auditor）补齐
  独立复核后才能占用限额；复核驳回即拒绝；限额余额不足同样不能批准。
- 紧急项目事后补算六维评分（不排名），保证追踪链路完整。

### 7. 政策换版模拟，不重算历史决定

- `GET /funding/simulate?round_id=&policy_version=` 使用另一版政策对**冻结证据**重新
  打分与模拟分配，只返回对**未批准项目**的排名/取舍变化；已批准、已结算项目不参与
  重排，历史状态、分数、政策版本原样保留（有测试覆盖）。

### 8. 一笔资金的全链路追踪

`GET /funding/trace?application_id=` 返回：冻结证据哈希、评分政策版本与各维得分理由、
排名、各级分配、独立复核、里程碑（计划/承诺/支付）、完整台账分录（带所属会计期间），
以及承诺/已付/追回/未付汇总。财政与公路部门通过 API 即可追踪一笔资金从**排名 →
占用 → 结算**的全过程。

## 目录

- `src/transport_coordination/`
  - `service.py` / `storage.py` / `audit.py`：基础登记、SQLite 与哈希审计链
  - `funding_service.py`：养护资金决策与执行领域服务
  - `funding_storage.py`：资金域表结构
  - `scoring.py`：纯函数、版本化、可解释评分引擎
  - `funding_models.py`：资金域数据对象
  - `api.py`：HTTP/JSON 边界（基础接口 + `/funding/*`）
  - `acceptance.py`：离线端到端验收
- `tests/`：基础服务测试 + 32 个资金域单元/HTTP/端到端用例

## 环境与测试

- Linux，Python 3.11+，运行时仅用标准库与 SQLite

```bash
python3 -m compileall -q src tests
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m transport_coordination.acceptance
```

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database service.sqlite3 --host 127.0.0.1 --port 8080
```

写接口通过 `X-Actor-Id` 标识操作者，所有写接口以 `request_id` 保证幂等。新增角色：
`highway`（公路部门）、`finance`（财政部门）、`reviewer`（独立复核，应登记在申报
单位之外的组织）。

### 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/funding/segments` | 登记路段（路线、桩号范围） |
| POST | `/funding/evidence` | 录入六维证据（可滚动更新） |
| POST | `/funding/policies` | 登记新版评分政策（不可改旧版） |
| POST | `/funding/rounds` | 创建申报轮次（截止时间、紧急限额） |
| POST | `/funding/envelopes` | 设置中央/省/县资金额度 |
| POST | `/funding/applications` | 申报项目（区间、窗口、依赖、里程碑） |
| GET  | `/funding/conflicts?round_id=` | 批准前查重结果 |
| POST | `/funding/freeze` | 截止：固化证据快照与政策版本并评分 |
| POST | `/funding/decide` | 批准组合（查重通过后按排名与资金约束分配） |
| POST | `/funding/emergency-review` | 紧急抢修独立复核 |
| POST | `/funding/payments` | 里程碑支付 |
| POST | `/funding/changes` | 项目变更（追加/缩减） |
| POST | `/funding/cancellations` | 项目取消（释放未付承诺） |
| POST | `/funding/recoveries` | 已结算项目结余/违规追回 |
| POST | `/funding/periods/open` · `/close` | 会计期间开关 |
| GET  | `/funding/rounds` · `/portfolio` · `/trace` · `/simulate` | 轮次余额、组合、全链路追踪、换版模拟 |

健康检查 `GET /health` 同时校验哈希审计链；服务重启后 SQLite 中的业务状态、冻结快照、
台账与审计历史继续保留。

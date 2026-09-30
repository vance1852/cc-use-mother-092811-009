# 分配普通公路养护资金协同基础服务

本项目提供综合交通运输业务共享的服务端基础能力，负责运营机构、交通节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域服务可以在这些稳定边界上扩展自己的状态、规则和接口。

在此基础上，`funding`/`funding_service` 模块实现**县域普通公路养护资金决策与执行服务**：汇集设施状况、服务人口、替代性、灾害暴露、历史维修、项目依赖与各级资金约束，在申报截止时冻结证据与评分政策，形成可解释的投资组合；批准前识别重复申报、同一路段拆项与互斥施工窗口；额度下达后按里程碑保留承诺与支付；变更、取消与结余回收不得改写已关账期间；紧急抢修走限额例外但必须经独立复核；财政与公路部门可通过 API 追踪一笔资金从排名、占用到结算的全过程，并在不重算历史决定的前提下模拟政策换版对未批准项目的影响。

## 目录

- `src/transport_coordination/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
  - `funding.py`：纯规则——评分政策校验与加权打分、重复/拆项/施工窗口识别、资金约束下的组合选择；
  - `funding_service.py`：轮次冻结、组合批准、账务分录、里程碑保留与支付、关账、紧急例外、资金追踪与政策换版模拟；
- `tests/`：基础规则、事务边界、接口路由、资金决策与端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

基础能力验收：

```bash
PYTHONPATH=src python3 -m transport_coordination.acceptance
```

养护资金决策与执行验收：

```bash
PYTHONPATH=src python3 -m transport_coordination.funding_acceptance
```

验收命令会在临时 SQLite 数据库中走通“申报 → 截止冻结证据与政策 → 排名与冲突识别 → 组合批准与承诺 → 里程碑保留与支付 → 关账不可改写 → 紧急抢修限额例外与独立复核 → 政策换版模拟”，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。其中乡村唯一通达且高灾害暴露的项目排名高于交通量大但替代路线多、灾害风险低的干线；偏向服务人口的换版模拟会改变候选决策，但不改变已落账的历史结果。

## 评分与取舍口径

- 六项因子均标准化到 0~100 再加权：设施状况、服务人口（对数饱和，避免规模一票否决）、替代性（唯一通达且无替代路线得满分）、灾害暴露、历史维修欠账、项目依赖协同。
- 权重在政策版本中归一化为和为 1；轮次冻结时把政策与证据快照（含哈希）一并绑定，此后打分依据不可变。
- 组合在中央/省/县三级额度（扣除紧急备用额度）内按排名选择；前置项目未入选则子项目不入选，任一硬冲突（重复/拆项/窗口）双方至多入选一个，未入选原因随排名一并保存以便说明取舍。

## 账务不变量

- 账务表只追加：`commit`（承诺）、`adjust`（带符号变更）、`pay`（扣除保留金后的实付）、`release`（保留金拨付）、`recover`（未用承诺回收）。
- 会计期间关账后，任何针对该期间的变更、取消、支付与回收一律拒绝；历史分录永不更新或删除。
- 里程碑按权重计费，按 `retention_pct` 预留保留金，全部里程碑核定支付后才允许结清保留金；取整差额形成的承诺结余在结清时自动回收。
- 紧急抢修只能在轮次批准后的执行期申报，受各级紧急备用额度上限约束，且必须由申报人之外的复核人独立通过后才形成承诺。

## 主要 HTTP 接口

- 政策：`POST/GET /policies`
- 路段与轮次：`POST /segments`、`POST /rounds`、`POST /allocations`、`POST /periods`、`POST /periods/close`
- 申报与决策：`POST /projects`、`POST /rounds/freeze`、`POST /rounds/approve`、`GET /rounds/{id}`、`POST /rounds/simulate-policy`
- 紧急例外：`POST /emergencies/review`
- 执行与支付：`POST /projects/start`、`POST /milestones/verify`、`POST /milestones/pay`、`POST /projects/release-retention`、`POST /projects/adjust`、`POST /projects/cancel`、`POST /projects/recover-savings`
- 全链路追踪：`GET /projects/{id}/trace`

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport_coordination.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，角色包括 `admin`、`finance`（财政）、`highway`（公路）、`reviewer`（独立复核）、`operator`、`auditor`。服务重启后 SQLite 中的业务状态和审计历史继续保留。

# 报价可靠性与运维

本次改造将价格检索、对象计量、工序覆盖、任务恢复和当前结果统一到服务端契约。恢复必须重新通过正常校验，不直接修改金额或把失败变为零元。

## 处理链路

| 环节 | 实现 | 约束 |
| --- | --- | --- |
| 价格查找 | `catalog_search.py` | 列表、Agent、材料分项包、候选抽屉共用检索；候选保留每种材料/板厚，记录检索完成与排除原因 |
| 来源与计量 | `requirements_schemas.py`、`manufacturing.py` | 部件使用稳定对象编号；外轮廓、局部尺寸、板厚、净面积和展开面积分开；记录来源、推导、假设 |
| 制作费用 | `process_taxonomy.py`、`manufacturing.py` | 工序按对象覆盖；丝印/UV、喷漆/烤漆、蚀刻/蚀刻填色分别匹配；材料费和电源费不能冒充全部制作 |
| 价格治理 | `CatalogContract` | 管理员维护用途、范围、适用工序、已含工序；保留导入列、原单元格及价格版本；配置参考不能直接成为整件单价 |
| 逐行生命周期 | `pricing_work_items`、`pricing_workflow.py` | 保存阶段、原因、责任方、事实/目录/规则/引擎依赖及恢复次数；停止、未授权、已批准任务不自动重开 |
| 当前结果 | `quote_workspace.py` | 从 SQL 当前事实重新计算；详情和轮询共用结果、原因及汇总；历史失效金额不补入当前结果 |
| 验收与运维 | 共享契约、CI、维护接口、独立 worker | 通过校验后保存；可审计重验、刷新候选、限定行重试；区分 API 存活与后台可接单 |

### 阻塞归属

- `customer_input`：已确认缺少客户事实，由需求方补充。
- `catalog_gap`：已查适用价格仍缺计费依据，由报价管理员处理。
- `recognition_review`：原件未核清、局部尺寸或归属冲突，由系统核图。
- `plan_repair`：单位、参数、公式、费用覆盖、引用版本等问题，由系统核价。
- `execution_error` / `service_unavailable`：执行或服务故障，由系统运维。

次数耗尽不能证明客户缺资料。诊断、原始出处和失败方案保留，技术错误不自动转交客户填写。

### 自动恢复

新一轮报价在用户授权后记录 `external_processing_authorized` 和 `auto_resume`。独立 worker 定期比对等待项目的事实、相关价格/规则版本和计价引擎版本。

同一依赖签名至多自动恢复一次。没有相关变化不重新付费调用；无关价格更新不重开客户事实问题。保存的方案先重编译、复算，必要时才继续模型处理。其余项目和人工修改保留。

显式停止和关闭自动恢复优先。历史任务不会因升级被批量授权重跑。管理员可以在报价结果的“处理”菜单重验当前结果、刷新候选、重试系统故障或关闭自动恢复。

## 启动与排障

Windows 项目根目录：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start-quote-services.ps1
```

默认前端 `http://127.0.0.1:3001`，API `http://127.0.0.1:8002`。启动脚本先执行追加迁移，启动 API、独立 worker 和前端，等待就绪检查，记录 PID 与日志。端口冲突时使用 `-ApiPort` / `-WebPort`，不会停止无关应用。若其他应用占用 IPv6 的 `localhost`，请使用明确的 `127.0.0.1` 地址。

```powershell
# 重启 API，已存在的独立 worker 继续运行
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/restart-api.ps1
# 仅启动/复用独立 worker
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start-quote-worker.ps1
# 仅恢复前端，沿用已有 API
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start-quote-frontend.ps1
```

其他部署可设置 `AGENT_WORKER_MODE=external`，分别运行：

```text
python -m uvicorn app.main:app --host 127.0.0.1 --port 8002
python -m app.agent_worker
```

`embedded` 保留旧部署兼容；`disabled` 仅用于明确不运行后台的环境。生产主机仍应使用自身的服务管理器负责进程退出后的重启和开机启动，本次没有安装系统服务或计划任务。

| 检查 | 用途 |
| --- | --- |
| `GET /api/health` | API/数据库存活 |
| `GET /api/health/ready` | Redis、迁移及有效 worker 心跳；不可接单返回 503 |
| `GET /api/operations` | 受管理口令保护的 worker、滞留任务和逐行状态 |
| `POST /api/operations/worker` | `draining` 控制暂停接新调度片段，保留当前片段及断点，记录理由和审计 |
| `GET /api/quotation-contract` | 引擎版本、状态、工序、参数容量 |
| `POST /api/jobs/{id}/maintenance` | 带当前 `revision`、`action`、`reason`，可选 `line_ids` 的受控维护 |

维护 action：`revalidate`、`refresh_candidates`、`retry_failed`、`disable_auto_resume`。鉴权沿用 `X-Admin-Token`。版本冲突返回 409，重试技术故障仍需既有外部处理授权，不覆盖已批准报价。

也可在 backend 目录通过 `python -m scripts.maintain_quote 24 25 --action revalidate --reason "升级后重验并登记状态"` 调用同一接口，避免直接修改数据库。

`.runtime/api.err.log`、`worker.err.log`、`web.err.log` 分别保存服务日志。SQL 保存业务进度，Redis 保存调度租约与进度镜像；重启后以 SQL 修复镜像。一个任务的 Redis 租约防止重复执行，项目内仍使用配置的四并发。

后台启动前端时，脚本仅对子进程设置 `CI=true`，禁用 Vite 在父终端标准输入关闭时自动停服的行为，启动后检查本项目入口；调用者的环境变量随后恢复。旧 `start.ps1` 已委托同一启动实现。

## 验收门禁

```powershell
cd backend
../.venv/Scripts/python.exe -m scripts.export_quote_contract --check
../.venv/Scripts/python.exe -m pytest -q --disable-warnings
cd ../frontend
npm.cmd test
npm.cmd run build
```

前端状态、工序和参数容量由后端生成，修改契约后运行 `python -m scripts.export_quote_contract`。CI 在 PostgreSQL/Redis 隔离环境执行迁移、契约检查、后端测试、前端测试和构建，不使用真实模型密钥。需要 Docker 的文件沙箱集成仍需独立启用，不能将跳过项当作已验收。

真实图片事故验收：

```powershell
node scripts/smoke-quote-workflow.cjs
```

需要本机可用的 Playwright 与 Chrome，默认访问 `127.0.0.1:3001`，可用 `QUOTE_WEB_URL` 改地址。脚本绑定任务 24/25 的原图 SHA-256，校验四种材料候选、原图预览、详情与轮询一致、历史无效金额不混入当前结果、问题责任方及桌面/手机排版。浏览器拦截全部非 GET 业务请求，不恢复付费报价。验收报告和截图写入 `.runtime/`。

测试分三层：纯计算/契约回归、隔离数据库与 Redis 流程测试、真实历史资料的只读浏览器验收。真实模型在任意新图片上的识别准确率仍需要样本持续评估，不能由脚本测试推导为百分之百。

2026-10-16 本机验收：后端全量 1127 项通过、7 项环境相关跳过；补充的维护/当前结果专项 45 项通过；前端 54 项通过及生产构建通过。任务 24/25 的只读真实图片验收通过，原图和历史报价没有改写。CI 配置已加入仓库，远程平台的首次执行仍由推送触发。

数据库已追加逐行工作表；迁移 `d034f7_restore_query_indexes` 幂等补齐历史本机缺失的 10 个既有查询索引，不修改任何价格、数量、原件或历史金额。

## 业务边界

目前真实立牌图保留了整体 520×2000 mm 和各板厚，可检索材料分项；完整报价仍需采购件数、钢材牌号、单双面、部件展开/逐字工程量及 LED 和加工的适用费用。系统不会以默认件数、通用损耗或其他案例的整套灯组价格补齐。

工序词表和结构化覆盖不是自动推导任意产品完整工艺的规则库。新增做法仍需录入适用计费依据与来源；缺少实际用量不会因维护工序标签而自动获得报价。搜索当前仍使用确定性全目录评分，尚未引入跨请求索引缓存；目录规模扩大后应以检索一致性验收约束索引化改造。

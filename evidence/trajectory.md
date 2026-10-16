# Webhook 配置切换时的失败投递隔离

> **Workspace:** F:\swe\098857\project-01

---

**User**

主办方把 webhook 地址或订阅范围从旧集成切换到新集成时，切换前已经失败并等待重试的通知不能被改写成发往新地址的新请求，也不能因为关联订单、票券或其他业务对象随后变化而改变原始内容。请让每次通知首次排队时取得稳定的投递身份，并保留当时的目标地址、事件类型和 JSON 请求快照；即时重试、数据库延迟重试、周期扫描以及管理员的“立即重试”都必须继续同一投递，而配置修改只影响修改后产生的新通知。网络异常或非成功响应进入既有退避流程时，worker 重启、重复周期扫描和人工操作并发不能产生两个活动副本，也不能丢失尚未成功的投递；任务发布失败后仍要留在可恢复状态。收到 2xx 后只记录一个成功终态，410 仍按现有规则停用 webhook 并结束该投递，管理员停用 webhook 或选择放弃重试时应明确终止相应待处理项。控制台日志需要把一次投递的各次尝试、冻结的目标和最终状态关联起来，使管理员能区分等待、正在尝试、成功、已放弃和因端点停用而终止；过期清理不得删除仍在重试窗口内的必要快照。没有配置切换或故障时，现有事件筛选、约三天的退避节奏、响应正文截断、WebhookCall 历史以及 REST 配置接口保持兼容。

**Assistant**

我先了解一下项目结构和现有的 webhook 实现，然后制定修改计划。

toolName: view_folder

status: failed

f:\swe\098857\project-01

toolName: file_search

status: success

toolName: file_search

status: success

这是 pretix 项目（Django 票务系统）。让我找到 webhook 相关的源码。

toolName: search_by_regex

status: success

核心文件是 `src/pretix/api/webhooks.py`。让我读取它和相关模型。

toolName: view_files

status: success

filePath: f:\swe\098857\project-01\src\pretix\api\webhooks.py

现在读取模型定义和相关测试。

toolName: search_by_regex

status: success

toolName: view_files

status: success

filePath: f:\swe\098857\project-01\src\pretix\api\models.py

让我读取现有的 webhook 测试，了解行为约定。

toolName: view_files

status: success

filePath: f:\swe\098857\project-01\src\tests\base\test_webhooks.py

toolName: view_files

status: success

filePath: f:\swe\098857\project-01\src\tests\api\test_webhooks.py

toolName: search_by_regex

status: success

toolName: view_files

status: success

filePath: f:\swe\098857\project-01\src\pretix\control\views\organizer.py

让我查找过期清理相关代码（WebHookCall 历史清理）和周期任务。

toolName: search_by_regex

status: success

toolName: view_files

status: success

filePath: f:\swe\098857\project-01\src\pretix\api\signals.py

toolName: file_search

status: success

toolName: search_by_regex

status: success

toolName: view_files

status: success

filePath: f:\swe\098857\project-01\src\pretix\api\migrations\0008_webhookcallretry.py

toolName: view_files

status: success

filePath: f:\swe\098857\project-01\src\tests\control\test_webhooks.py

让我查看触发链路、模板以及 LogEntry 的 webhook_type 属性。

toolName: search_by_regex

status: success

toolName: view_files

status: success

filePath: f:\swe\098857\project-01\src\pretix\base\models\log.py

让我查看 API 视图/序列化器、celery 配置和周期任务节奏。

toolName: view_files

status: success

filePath: f:\swe\098857\project-01\src\pretix\api\views\webhooks.py

toolName: view_files

status: success

filePath: f:\swe\098857\project-01\src\pretix\api\serializers\webhooks.py

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

toolName: view_files

status: success

filePath: f:\swe\098857\project-01\src\pretix\base\management\commands\runperiodic.py

toolName: search_by_regex

status: success

toolName: file_search

status: success

toolName: view_files

status: success

filePath: f:\swe\098857\project-01\src\pretix\control\templates\pretixcontrol\organizers\webhook_logs.html

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

toolName: view_files

status: success

filePath: f:\swe\098857\project-01\src\pretix\base\services\tasks.py

toolName: view_files

status: success

filePath: f:\swe\098857\project-01\src\pretix\api\migrations\0013_alter_webhookcallretry_retry_not_before.py

toolName: view_files

status: success

filePath: f:\swe\098857\project-01\src\pretix\api\migrations\0014_alter_webhook_target_url_and_more.py

让我查看测试配置中 celery 的执行方式，以及 base.py 中触发 notify_webhooks 的位置。

toolName: search_by_regex

status: success

toolName: view_files

status: success

filePath: f:\swe\098857\project-01\src\pretix\base\models\base.py

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

toolName: file_search

status: success

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

我已经理解了整体架构。让我再确认几个引用点，然后制定计划。

toolName: search_by_regex

status: success

现在我对全貌已经清楚了。创建任务计划。

toolName: todo_write

status: success

Todos updated: 6 items

先看 models.py 头部和 WebHook 模型周边的工具。

toolName: view_files

status: success

filePath: f:\swe\098857\project-01\src\pretix\api\models.py

检查 celery 任务超时限制，以确定“僵死认领”的合理超时。

toolName: search_by_regex

status: success

检查 pretixbase 最新迁移编号（用于新迁移依赖）。

toolName: search_by_regex

status: success

toolName: search_by_regex

status: success

最新 pretixbase 迁移是 0311。现在开始实现。先改模型层：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098857\project-01\src\pretix\api\models.py

changes: undefined

现在替换 WebHookCall / WebHookCallRetry 部分：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098857\project-01\src\pretix\api\models.py

changes: undefined

模型层完成。现在编写迁移文件：

toolName: Write

status: success

filePath: f:\swe\098857\project-01\src\pretix\api\migrations\0015_webhookdelivery.py

changes: undefined

把索引放进 CreateModel 的 options 里，与 `makemigrations` 自动生成的格式保持一致：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098857\project-01\src\pretix\api\migrations\0015_webhookdelivery.py

changes: undefined

toolName: todo_write

status: success

Todos updated: 6 items

现在重写 `webhooks.py` 的任务部分。这是核心改动：

toolName: view_files

status: success

filePath: f:\swe\098857\project-01\src\pretix\api\webhooks.py

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098857\project-01\src\pretix\api\webhooks.py

changes: undefined

现在替换整个任务实现段：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098857\project-01\src\pretix\api\webhooks.py

changes: undefined

核心任务重写完成。现在更新控制台视图（放弃重试改为显式终止、retry_count 改为等待中的 delivery）：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098857\project-01\src\pretix\control\views\organizer.py

changes: undefined

更新视图中的模型导入：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098857\project-01\src\pretix\control\views\organizer.py

changes: undefined

现在更新过期清理逻辑：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098857\project-01\src\pretix\api\signals.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098857\project-01\src\pretix\api\signals.py

changes: undefined

主要代码改完了。现在运行现有测试验证。先确认环境：

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

环境里有个无关的 ckan pytest 插件加载失败。查一下插件名并单独禁用：

toolName: Shell

status: success

toolName: Shell

status: success

toolName: view_files

status: success

filePath: f:\swe\098857\project-01\src\setup.cfg

环境缺少 `pytz_deprecation_shim`，用命令行覆盖 filterwarnings：

toolName: Shell

status: success

环境缺少一些依赖。尝试安装缺失包：

toolName: Shell

status: success

toolName: Shell

status: success

依赖缺失较多。查看项目依赖并尝试以可编辑模式安装：

toolName: search_by_regex

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

缺失依赖较多，直接安装项目声明的全套依赖（版本固定，已装的会跳过）：

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: search_by_regex

status: success

toolName: view_files

status: success

filePath: f:\swe\098857\project-01\pyproject.toml

toolName: Shell

status: success

toolName: Shell

status: success

基础和 API 测试通过。控制台测试有 4 个 setup 错误，查看详情：

toolName: Shell

status: success

错误来自全局 pytest-flask 插件（与本项目无关）。禁用它再跑：

toolName: Shell

status: success

现有测试全部通过。现在编写新行为的专项测试。先标记进度：

toolName: todo_write

status: success

Todos updated: 6 items

toolName: Write

status: success

filePath: f:\swe\098857\project-01\src\tests\base\test_webhook_deliveries.py

changes: undefined

修正两个测试中过于绕弯/可能不确定的断言：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098857\project-01\src\tests\base\test_webhook_deliveries.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098857\project-01\src\tests\base\test_webhook_deliveries.py

changes: undefined

运行新测试文件：

toolName: Shell

status: success

`no_celery_phase_retry` fixture 在整个测试期间生效，导致第二次入队也不发布。改为只在第一次 fanout 期间打补丁：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098857\project-01\src\tests\base\test_webhook_deliveries.py

changes: undefined

toolName: Shell

status: success

全部 20 个新测试通过。再为控制台“立即重试/停止重试”按钮补充视图级测试：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098857\project-01\src\tests\control\test_webhooks.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098857\project-01\src\tests\control\test_webhooks.py

changes: undefined

toolName: Shell

status: success

全部通过。现在检查迁移一致性和 Django 系统检查：

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

测试配置禁用了迁移模块。看一下 tests/settings.py 如何配置，再用启用迁移的设置检查：

toolName: view_files

status: success

filePath: f:\swe\098857\project-01\src\tests\settings.py

testutils 为提速禁用了迁移。脚本里临时覆盖该设置后检查：

toolName: Shell

status: success

两处不一致：索引名和 `retry_not_before` 缺少 default。修正迁移：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098857\project-01\src\pretix\api\migrations\0015_webhookdelivery.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098857\project-01\src\pretix\api\migrations\0015_webhookdelivery.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098857\project-01\src\pretix\api\migrations\0015_webhookdelivery.py

changes: undefined

toolName: Shell

status: success

历史迁移状态中的索引必须显式命名。用 dry-run 给出的自动名称：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098857\project-01\src\pretix\api\migrations\0015_webhookdelivery.py

changes: undefined

toolName: Shell

status: success

toolName: Shell

status: success

迁移与模型一致。现在验证迁移能在全新数据库上实际执行（含数据迁移代码）：

toolName: search_by_regex

status: success

toolName: Shell

status: success

迁移正向/回退/再迁移全部通过。运行更大范围的回归测试：

toolName: Shell

status: success

再跑整个 api 测试目录和订单测试，覆盖所有触发日志/webhook 的路径：

toolName: Shell

status: success

输出被环境问题刷屏了。获取失败测试清单和汇总行：

toolName: Shell

status: success

4 个失败看似与环境（Windows 文件锁/德语 locale）有关，但 410 状态码需要确认是否与我的改动有关。查看该测试：

toolName: search_by_regex

status: success

这些是 `transaction=True` 的真实文件导出任务，在 Windows 上因临时文件锁失败导致 410。用 git stash 在干净代码上验证它们本来就失败：

toolName: Shell

status: success

toolName: Shell

status: success

4 个失败在干净基线上同样存在，确认为环境预存问题。恢复我的改动：

toolName: Shell

status: success

改动已恢复。运行 lint 检查（项目 CI 使用 flake8）：

toolName: Shell

status: success

toolName: Shell

status: success

toolName: Shell

status: success

toolName: view_files

status: success

filePath: f:\swe\098857\project-01\src\tests\base\test_webhook_deliveries.py

toolName: view_files

status: success

filePath: f:\swe\098857\project-01\src\tests\base\test_webhook_deliveries.py

toolName: view_files

status: success

filePath: f:\swe\098857\project-01\src\tests\base\test_webhook_deliveries.py

修复 lint 问题：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098857\project-01\src\tests\base\test_webhook_deliveries.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098857\project-01\src\tests\base\test_webhook_deliveries.py

changes: undefined

toolName: edit_file_search_replace

status: failed

filePath: f:\swe\098857\project-01\src\tests\base\test_webhook_deliveries.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098857\project-01\src\tests\base\test_webhook_deliveries.py

changes: undefined

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098857\project-01\src\tests\control\test_webhooks.py

changes: undefined

toolName: Shell

status: success

Lint 通过。再完整跑一遍 webhook 相关测试确认修改无误：

toolName: Shell

status: success

全部通过。还有一处模板文案需要更新——旧文案说“5 分钟内的重试无法停止”，新机制下所有等待中的投递都可被停止/立即重试：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098857\project-01\src\pretix\control\templates\pretixcontrol\organizers\webhook_logs.html

changes: undefined

为让管理员在控制台页面也能直接区分五种状态，在日志页增加待处理/已终止投递列表。先改视图上下文：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098857\project-01\src\pretix\control\views\organizer.py

changes: undefined

确认 organizer.py 中 `now` 和 `timedelta` 已导入：

toolName: search_by_regex

status: success

现在在模板中添加投递状态区块：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098857\project-01\src\pretix\control\templates\pretixcontrol\organizers\webhook_logs.html

changes: undefined

为该页面补一个渲染测试：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098857\project-01\src\tests\control\test_webhooks.py

changes: undefined

toolName: Shell

status: success

toolName: Shell

status: success

用 IDE 诊断检查改动文件有无静态错误：

toolName: GetDiagnostics

status: success

最后通读一遍核心文件确认逻辑完整：

toolName: view_files

status: success

filePath: f:\swe\098857\project-01\src\pretix\api\webhooks.py

toolName: search_by_regex

status: success

检查文档中是否描述了旧的重试机制需要同步：

toolName: search_by_regex

status: success

toolName: view_files

status: success

filePath: f:\swe\098857\project-01\doc\api\webhooks.rst

文档行为保持兼容。补充一小段说明投递快照语义，便于运维理解配置切换：

toolName: edit_file_search_replace

status: success

filePath: f:\swe\098857\project-01\doc\api\webhooks.rst

changes: undefined

最后再跑一轮全部相关测试，包括启动检查：

toolName: Shell

status: success

test_startup 失败是子进程网络初始化错误（WinError 10106）。在基线上验证：

toolName: Shell

status: success

确认 test_startup 在干净基线上同样失败，属环境问题。完成任务清单：

toolName: todo_write

status: success

Todos updated: 6 items

已完成。核心设计是把"一次通知对一个 webhook 的投递"建模为稳定的 `WebHookDelivery`，所有重试路径共用它。

## 改动概览

**新模型（[models.py](file:///f:/swe/098857/project-01/src/pretix/api/models.py#L167-L235)）**
- `WebHookDelivery`：首次排队时创建，持有稳定 ID 与**冻结**的 `target_url`、`action_type`、`payload`（JSON 快照），并有五态状态机 `waiting / running / success / given_up / disabled`、`retry_count`、`retry_not_before`、`scheduled_at`（发布去重标记）、`locked_at`（认领/僵死检测）；`(webhook, logentry)` 唯一约束防止重复投递。
- `WebHookCall` 历史增加可空 `delivery` 外键（SET_NULL，历史不受影响）；旧的 `WebHookCallRetry` 移除。
- `WebHook.save()` 在 enabled 由 true→false 时，把所有未终态投递显式置为 `disabled`（覆盖控制台停用、REST 删除/停用、410 各路径）。

**投递流程（[webhooks.py](file:///f:/swe/098857/project-01/src/pretix/api/webhooks.py#L489-L936)）**
- `notify_webhooks` 入队时构建一次 payload 并连同目标 URL、事件类型落库；之后订单/票券等业务对象再变化、主办方改 URL 或订阅范围，都不影响已排队投递，配置只对**新**投递生效。即时重试、DB 延迟重试、周期扫描、管理员"立即重试"全部只带 `delivery_id` 继续同一投递。
- `send_webhook` 先在 `select_for_update(skip_locked)` 下行级认领（waiting→running）：终态不再发、并发重复任务退让、运行超 10 分钟的僵死锁可被回收；HTTP 用冻结快照字节发送，终局在持锁事务中落定——2xx 只产生一个 `success`，410 停用 webhook 并终止，退避节奏仍为 5s→…→24h（约三天）、<5 分钟走 Celery、≥5 分钟落库，发尽则 `given_up`，响应正文仍截断 1MB。
- 周期扫描只挑到期且无近期发布标记的 waiting 行与僵死 running 行；任务发布失败时清除标记，投递保持可恢复，不丢件；任务发布失败也不产生两个活动副本。

**终止与运维**
- 管理员"停止重试"改为把 waiting/running 投递显式置为 `given_up`（保留快照与终态），见 [organizer.py](file:///f:/swe/098857/project-01/src/pretix/control/views/organizer.py#L1628-L1661)；日志页新增投递状态表，可区分等待/尝试中/成功/已放弃/端点停用。
- 每条日志都带 `webhook delivery #id (webhook=, event=, target=)` 前缀，串联各次尝试与最终状态。
- 清理任务（[signals.py](file:///f:/swe/098857/project-01/src/pretix/api/signals.py#L69-L80)）只删除超 30 天且已终态的投递，waiting/running 的必要快照永不删；WebHookCall 历史仍按 30 天清理且 SET_NULL 保留。

**迁移（[0015_webhookdelivery.py](file:///f:/swe/098857/project-01/src/pretix/api/migrations/0015_webhookdelivery.py)）**：建新表、给 WebHookCall 加字段、尽力把旧 retry 行冻结快照迁入新表、删旧表；已验证全新库正向/回退/再迁移及 `makemigrations --check` 一致。

**验证**：新增 28 个场景测试（冻结不被改写、业务对象后续变化、重复任务单发、worker 崩溃回收、发布失败可恢复、410/停用/放弃终止、扫描去重、清理保护等）；webhook 相关 44 项全部通过；`tests/api` + 订单共 1879 项回归通过。仅有的几个失败（exporter/shredder 文件锁、德语 locale、startup 网络）已在干净基线上复现，属本机 Windows 环境问题。REST 配置接口与事件筛选、退避节奏、WebhookCall 历史均保持兼容。

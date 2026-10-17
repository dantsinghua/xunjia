# 现券询价 V2.2：正式入口与切换边界
正式启动入口仍是 server.app:create_app，但已统一使用 server/negotiation 下的新核心。
没有运行时开关把新入口回退到旧 xj_* 业务表；所有账号绑定、RFQ、原文、报价、控制和命令状态写入 xj2_*。
server/legacy_app.py、legacy_schema.py、legacy_health.py 只保留为明确命名的历史回归测试对象；不可作为生产启动目标。
## 核心链路
1. 接收原始消息，立刻提交原文和线程消息水位；前端原文获取与结构化分析异步。
2. Chatflow A 基于真实上下文产生多券、双边价量、引用和谈判意图，后端验证证据并独立提交事实。
3. Chatflow B 基于已提交事实和组目标给出候选；校验价格、量、正文和依赖版本后才建立命令。
4. OSS/Mail执行适配只接受匹配账号与版本的命令；采集端在原生发送前核查消息水位。
5. 未知回执只对账，不换命令ID重发；确定失败/取消清理对应pending，迟到成功只记录已发事实。
暂停只关闭发送，解析和消息展示不停止。上下文默认20条（全局范围10..30）。
每券按目标方向汇总各对手当前有效、达价且最终确认的整量，>=目标量可提前交人工。
到价仍可改善时继续谈；不自动拆一家整量，不自动成交。
跨日必须向对手本人确认当天价格；我方绝对限价沿用，BP限价按最新交易日中债估值重新计算。
净价/全价必须取得对手确认的结算日，并使用已验收条款的计算结果；条款未配置保留原价但不伪造收益率。
## 新表（与旧库不是就地自动迁移）
sql/oceanbase/001_xunjia_schema_mysql.sql 是当前正式的15张V2.2表、13个二级索引。
sql/oceanbase/archive/001_legacy_schema_mysql.sql 仅供历史对照，不是部署入口。
OceanBase部署不会在服务启动时自动建表；由运维明确选择专属库并执行SQL。
测试显式SQLite按当前schema创建，无真实客户数据。
## Dify
使用 dify/chatflow_v22/analysis.yml 与 reply.yml，两份Dify0.15.3兼容Chatflow，各3个基础节点。
不依赖Agent插件、私有回调票据或Dify管理端API；公开Bond Tool无需Token。
运行仅需要普通 App API Key：
- XUNJIA_DIFY_URL
- XUNJIA_DIFY_ANALYSIS_APP_KEY
- XUNJIA_DIFY_REPLY_APP_KEY
旧31节点兼容稿和 /internal/agent/* 的旧turn_ref协议不再写业务状态，返回410要求使用新合同。
本次源码切换不会自动创建、覆盖或发布现役Dify应用，也不声称模型语义/真实API耗时已验收。
## 不制造运行条件
XUNJIA_AGGREGATION_PROFILE 必须指向经过真实成功解析+回复耗时测量产生的JSON；
字段为quiet_ms、max_wait_ms、reference_ms、measurement_id，满足0<=quiet_ms<=max_wait_ms<reference_ms/2。
源码及配置模板不填800/1500/3000等未经测量的默认值。
缺少两阶段Key或测量报告时健康接口明确报告analysis_blockers，不能把代码测试通过说成真实对话已运行。
XUNJIA_ENABLE_LIVE_TRANSPORT 默认0；即使开启，采集宿主还必须明确公布 negotiation-v22-watermark 能力。
旧QTrade宿主不具备此能力时不得绕过此门槛发送。
## 分组验收
scripts/check_v22_release.py 每次只运行一个组，默认100秒上限：
    python scripts/check_v22_release.py --group cutover --reports /tmp/approved-check
组名：cutover、core、acceptance、developer、release、root、web-python、web-contract、static-web、web-build、web-unit。
全部执行后用 --group finalize 核对收集到的测试ID并集，拒绝任何失败、跳过或源码漂移。
历史兼容测试明确导入legacy_app；cutover、公共Tool、健康、前端HTTP合同等直接使用正式server.app。
测试模型与原生发送均为合成替身；代码测试不是实盘、真实Dify语义或阿里云OSS权限验收。
## GitHub初始化
原Git历史含外部二进制及现场数据，新GitHub main使用审核后的父提交为空的源码快照。
GitLab旧历史保留本地备份分支和gitlab remote。原现场配置、联系人、游标和大二进制不删除、不上传。
# 安装后现场验收入口

在专用安装前缀执行：

    /home/xunjia/venv/bin/python /home/xunjia/app/deploy/site_acceptance.py --prefix /home/xunjia

此默认检查只读取已安装文件、现场配置和健康接口，不连接模型、不执行DDL、不改服务、不发送IM。检查安装文件完整性、真实端口上的服务名、build_id、release_id与同一次boot_id，分别报告存活、业务就绪和自动化链路状态。默认未执行模型测试会列为 REAL_MODEL_PROBE_NOT_RUN；返回2表示完整放行条件未满足，不等于文件安装失败。

现场模型链路具备访问条件后，执行：

    /home/xunjia/venv/bin/python /home/xunjia/app/deploy/site_acceptance.py --prefix /home/xunjia --probe-dify

使用现场已经填写的两个普通App API Key，运行六个合成报价场景；真实调用Dify，但只创建随后清理的临时SQLite，不写客户业务库、不构造IM发送器。一个场景失败即停止，不批量重复请求故障上游。该选项可能产生模型调用费用。仅输出固定安全错误码，不输出App Key或上游原文。客户不需要提供Dify管理员权限。

六例覆盖估值省略BP、显式BP对照、全角符号、双边不同偏移、合法负收益率；原47例模型语料未替换或弱化。完整研发模型验收使用 scripts/measure_dify_v22.py --review-regressions，加上原47例共53例；少量现场探针不能当作全面自然语言准确率证明。

## 放行边界

本入口不会把 automation_ready 改为真。本版自动化接口仍要求后续真实跨端验收接线，当前未验证时保持503。它不能以某次模型成功替代同版QTrade安装、STS临时凭据、指定OSS桶、测试对手的原生发送和回执。

上线仍须完成：当前版OceanBase新表及并发合同；最终包真实安装/升级/回滚和就绪；客户主数据、中债估值、条款/日历/授信配置；明确测试对手的真实收发；现场物理ARM验证。安装器不修改Nginx、njcb-all或客户数据库角色。

## 故障定位

DIFY_TLS_ERROR 表示应用直连Dify的TLS故障；DIFY_UPSTREAM_TLS_ERROR 表示Dify已响应、其模型上游出现可识别TLS故障。鉴权/限流也分别标识Dify自身或模型上游。未知错误仍保留DIFY_HTTP_ERROR，原始不可信响应不会直接回显。

合成的数据库不可达安装测试，只能证明最终包能安装、启动并准确报DATABASE_UNAVAILABLE；不能登记为OceanBase实库通过或业务就绪。

配置示例和交付包始终默认关闭后台与真实发送。所有验收必须记录当前源码与build_id，不借用旧版本成绩。

# 麒麟 aarch64 完整离线交付准备（2026-10-18）

安琳要求最终重建独立的内网麒麟 aarch64 应用包，包含独立 Python、全部应用依赖、构建好的前端和配套组件。本记录区分准备验证与最终包验收；最终源码、模型统计和文档冻结前，不把旧 candidate 改名为最终包。

## 实际环境与来源

既有 ibquote 部署资料的历史现场为 Kylin V10 SP3 / Halberd / aarch64 / glibc 2.28 / 系统 Python 3.7.9。本次没有写入或执行其安装脚本。

本次使用的官方镜像为 `cr.kylinos.cn/kylin/kylin-server-platform:v10sp3`，ARM manifest 为 `sha256:8b0496126f596b44f92da0f117a71e1af290e62037ea577f8b0b388c54d41b30`。单 rootfs layer 66,730,583 字节，SHA256 `08cdddf81713f7492a75b271c9290b840fc32696eecfe58a74126384fa2d0b96`；独立方已核官方来源、linux/arm64 manifest 和下载字节。

实际 `.productinfo` 为 `Kylin Linux Advanced Server release V10 (SP3) /(Lance)-aarch64-Build23.02/20230324`，镜像元数据构建时间 `2026-10-10T17:22:56.43012458Z`（北京时间 2026-10-11）；实际执行显示 glibc 2.28、系统 Python 3.7.9。它是官方麒麟用户态，运行于 QEMU/PRoot 和 WSL/x86_64 宿主内核，不能写成客户物理 ARM 机器原生验收。此前 openEuler/glibc 2.28 验证保留为 ABI 仿真历史，不用它代替麒麟记录。

## 已落实的交付改动

- 新增 `deploy/bootstrap_runtime.py`，保持 Python 3.7 标准库兼容。执行包内解释器前核架构/glibc、双清单与实际 SHA256、受限归档和 ARM ELF64；只解本次 0700 临时根，不改系统 Python、PATH 或 PREFIX，结束清理。
- `deploy/entry.sh` 支持从仅有系统 3.7.9 的 ARM 目标引导包内 3.12。`deploy/05-test-package.sh` 也使用固定的包测试模式，无需手工解包运行时或联网安装。
- 实际应用安装仍由原 manager 在 `PREFIX/python` 放置永久运行时，venv 不依赖临时引导目录。已有运行时和原安装/回滚事务边界保留。
- `Manager.prepare_release` 在包有 `dify/` 时将它复制到 release/app，且纳入原 payload 哈希；原无此目录的旧包保持兼容。修复安装后测量脚本无法导入 Dify 测量模块的问题。
- 构建器保留精确纳入采集宿主 manifest 的逻辑；包中配套 collector runtime wheel、新宿主源码、独立测试和人工接线资料。QTrade 缺前像的限制不因打包而消失，不声称可自动升级任意 QTrade 版本。
- 安装说明已更新现役 V2.2 的 15 张表、13 个二级索引及普通 App Key/公开 Tool 免 Token、默认真实发送关闭的边界。复用客户已有 OceanBase/Dify，安装器不执行客户 DDL。
- 构建器补入原 `tests/conftest.py`，让包内测试继承收集前环境清理、临时状态和离线网络限制；该文件同时进入源码摘要及完整性清单，原测试隔离规则不变。

## 实际验证

独立 CPython3.12.13+20260805 的 aarch64 runtime 为 85,136,635 字节，SHA256 `c2083943c86dbb21ca0211238362fd922de7b0475688f26c135cf5d20a1c2f48`，与既有已保留的离线运行时字节一致。

在上述官方麒麟用户态中已实际完成新 ARM venv、33 个锁定运行/QA 依赖及 collector2.2.0 wheel 的离线 no-index 安装、pip check、34 个依赖/扩展模块及 collector 导入、SM4 运算；全部 exit 0，输入 hash 无漂移。还实际以麒麟系统 Python3.7.9 运行 entry/helper，校验并引导 ARM3.12.13，确认 PATH 不改、PREFIX 不写且临时解释器清理。该引导专项使用合成固定 manager，仅证明引导和隔离，不替代完整应用包测试。

本地开发/独立门：引导和 QA 路由 30 项独立通过；原 release 73 项通过；安装后测量入口真实 `ModuleNotFoundError: dify` 红测修复后，新开发2项加 release 同次75项通过。没有修改或弱化独立断言；计数存在包含关系，不累加。

包内测试隔离文件的收录回归先红，修复后与原 release 同次74项通过。独立最小包形验证确认完整性检查通过、篡改该文件后拒绝，并在污染环境中实际执行两项隔离测试；5个相关输入无漂移，临时根已清理。这是收录专项证据，不替代最终 tar 的完整 ARM 验收。

## 最终包待执行

最终统一门、模型47及恢复验证完成并冻结后，重新构建包含真实 `--arm-runtime` 的包，核 SHA256、SOURCE-HASHES 和完整清单，安全解到新目录，再在官方麒麟用户态从系统3.7.9调用包内 `05-test-package.sh`。该入口必须完成原完整包内用例、离线安装和 pip check；保留失败和清理证据，不用旧候选的 x86 结果替代。

工作区外 `ARM-PREPARATION.md`、`arm-preparation/kylin-dependencies-result.json`、`real-kylin-python37-bootstrap-result.json` 和独立 `KYLIN-ARM-ACCEPTANCE.md` 保存准备阶段证据；新包产物与完整QA结果见下方r20补记，准备成绩不替代新包结果。

## r20 最终候选包实际验收补记

上述待执行步骤现已在全仓2320项Python、69项Node及前端构建通过后执行。新包 `xunjia-v2.2-r20-kylin-aarch64-offline.tar.gz` 为208691951字节，SHA256 `9e163a84ae53f21f708443c66e7063fe83d03512325947845842e634ae46741a`，build_id `ffe4c9f09949684e8ff0b354f5fb60f4140599c77ba5e6c254cc409337d5f970`。运行时、依赖、采集wheel来自重新核实的既有字节，源码/构建前端/四画布/实测聚合配置及测试来自当前冻结树，不是旧包改名。

同一官方麒麟用户态和PRoot5.5.0中，实际从系统Python3.7.9调用包内05-test-package.sh，六阶段均exit0：完整性校验、创建venv、no-index/no-deps离线安装、pip check、1257节点收集、1257测试全部通过。测试342.32秒，完整包QA441.393秒；344输入零漂移，临时工作根清理，P0外层无残留。包内五范围包含release/acceptance/cutover/negotiation/benchmarks，不需要Node或联网下载。

现场数据库与真实IM仍单列，物理ARM现场未验证。当前源码增量交付及OceanBase QA启动地址问题见[最终交付记录](2026-10-18-final-delivery.md)，不将包通过当作这些事项完成。

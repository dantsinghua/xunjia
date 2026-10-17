# 现券询价谈价机器人 · 询价工作台(web)

南银理财现券询价项目前端工作台。用 **Vue3 + Vite + ant-design-vue + vue-router + dayjs** 把设计稿
`现券询价机器人.dc.html`(Claude Design 画布)逐屏还原成可运行应用,工具链与目录结构照抄
`bondtrade/web`(`api.ts` / `router.ts` / `theme.ts` / `App.vue`)。

## 三屏(顶部导航切换)

| 路由 | 屏 | 内容 |
|---|---|---|
| `/#/quote` | **意向询价** | 左筛选栏(方案/报价类型/清算/标签/类型/期限 + 价格区间)、中行情主表(债券基本信息3列 + 内部意向4列 + 外部报价6列 + 状态,13 子列,sticky 首末列)、勾选浮条(开始/停止询价 + 底价统一调整 ±BP)、债券详情弹窗、**v1.0 三参询价策略弹窗** |
| `/#/monitor` | **机器人监控** | 机器人开关+统计条、左对手×券卡片(同对手多券标「同一线程⚠」)、中聊天流(bot/对方/人工/系统气泡,券锚高亮、意图标签、点消息看链路追踪)、同页人工发送框、询价明细/导出弹窗 |
| `/#/chat` | **智能问答** | 左快捷问题 chips + 历史会话,右聊天(markdown 答案 + 柱/折/散点图,轻量 SVG 绘制) |

## 设计语言

南银深红 `#8A2823`、玻璃拟态(`backdrop-filter`)、`Noto Sans/Serif SC` + `JetBrains Mono`、
背景 `#F3F0EA`、右上「安琳」在线徽标、背景动画光斑。字体经 Google Fonts 引入(见 `index.html`)。

## 询价策略(v1.0 口径)

策略弹窗按 v1.0 需求书三参重写(**不是**设计稿旧的对手让步空间/最小可接受量/最大价差):

- **起始偏离(BP)** / **探价边界(BP)** / **步幅(BP)**
- **严格匹配报量** 开关
- 系统级 **S1 询价量取整档位** / **S2 跨日阈值(BP)**
- 派生显示 **轮次上限 =(起始 − 边界)/ 步幅 + 1**

## 启动

```bash
cd web
npm install     # 首次
npm run dev     # http://localhost:5190
```

- Vite 端口 **5190**;`/api` 代理到 `http://localhost:8120`(mock 后端,见同仓 `mock/`)。
- 生产构建:`npm run build`(先 `vue-tsc --noEmit` 类型检查,再 `vite build`)。

## 接口与 Mock 兜底

- `src/api.ts` 按 `docs/02-frontend-contract.md` 契约封装全部接口,一律 `{ ok, data }`。
- 后端字段口径(`cp_id` / `spread_bp` / `my_yield` / `value+unit` 等)与前端模型不同,
  在 `api.ts` 里逐端 `normalize`(照 bondtrade 的 `normalizeInstr` 套路)。
- **后端未就绪时自动回退内置 mock**(`src/lib/mock.ts` + `withMock`),后端没起也能看三屏、可截图。
- 内置 mock 移植自设计稿 `renderVals()`:12 只债券、4 个对手(含经纪人「华创证券刘素」)、
  带券锚/意图标签/链路追踪的谈价会话、种子随机的成交/历史/盘中曲线、规则匹配的问答+图表。

## 目录

```
src/
  api.ts              接口封装 + 后端→前端字段 normalize + mock 兜底
  theme.ts            antd 主题 + 品牌色 + 状态色
  styles.css          全局样式(玻璃拟态/动画/字体/表单)
  router.ts           三屏路由(hash 模式)
  App.vue             壳:玻璃顶栏 + 三屏导航 + 安琳徽标 + 时钟 + 背景光斑
  lib/mock.ts         内置 mock 数据与生成器(移植 renderVals)
  pages/
    Quote.vue         屏1 意向询价
    Monitor.vue       屏2 机器人监控
    Chat.vue          屏3 智能问答
  components/
    BondModal.vue     债券详情弹窗
    StrategyModal.vue 询价策略弹窗(v1.0 三参)
    TraceModal.vue    数据链路追踪弹窗
    Chart.vue         轻量 SVG 图表(柱/折/散点)
```

## 自测结果

- `npm run build` 通过(`vue-tsc` 零类型错误 + `vite build` 成功)。
- `npm run dev` 起 5190,`curl localhost:5190` 返回 200。
- Playwright 截图三屏 + 各弹窗均正常;与 `mock/`(8120)联调,行情表、监控聊天流(券锚高亮/意图标签)、
  债券详情(各渠道最优/在聊对手)、问答图表(柱/散点)全部渲染正确。

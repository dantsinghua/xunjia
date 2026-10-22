import type { ThemeConfig } from 'ant-design-vue/es/config-provider/context'

/** 南银理财现券询价工作台 · 设计语言(还原设计稿 现券询价机器人.dc.html)
 *  品牌深红 #B01C24,玻璃拟态,背景 #F3F0EA,统一离线系统字体。 */
export const NY = {
  red: '#B01C24',
  redBright: '#C2333B',
  redDeep: '#7C231F',
  paper: '#F3F0EA',
  panel: '#F6F1EC',
  card: '#FFFFFF',
  line: 'rgba(176, 28, 36,.12)',
  lineStrong: 'rgba(176, 28, 36,.3)',
  ink: '#2B2320',
  muted: '#6E6258',
  faint: '#8A7F76',
  fainter: '#A89C90',
  green: '#1F6B3E',
  greenBright: '#2E8B57',
  blue: '#1E5FB4',
  amber: '#8F6412',
  gold: '#7A5A2E',
} as const

/** 状态色:待询价(琥珀) / 询价中(蓝) / 转人工(深红) —— 与 renderVals ST 一致 */
export const STATUS_COLOR: Record<string, [string, string, string]> = {
  待询价: ['rgba(176,138,46,.14)', '#8F6412', 'rgba(176,138,46,.3)'],
  询价中: ['rgba(30,95,180,.12)', '#1E5FB4', 'rgba(30,95,180,.3)'],
  发送失败: ['rgba(176, 28, 36,.16)', '#B01C24', 'rgba(176, 28, 36,.4)'],
  转人工: ['rgba(176, 28, 36,.12)', '#B01C24', 'rgba(176, 28, 36,.35)'],
  已报价: ['rgba(15,118,110,.12)', '#0F766E', 'rgba(15,118,110,.3)'],
  还价中: ['rgba(199,122,18,.14)', '#C77A12', 'rgba(199,122,18,.3)'],
  待人工确认: ['rgba(176, 28, 36,.12)', '#B01C24', 'rgba(176, 28, 36,.35)'],
  已成交: ['rgba(46,125,79,.12)', '#1F6B3E', 'rgba(46,125,79,.3)'],
  已暂停: ['rgba(143,100,18,.14)', '#8F6412', 'rgba(143,100,18,.3)'],
  未成交: ['rgba(110,98,88,.12)', '#5C5148', 'rgba(110,98,88,.3)'],
}

const FONT_SANS = 'var(--font-ui)'

export const antdTheme: ThemeConfig = {
  token: {
    fontFamily: FONT_SANS,
    colorPrimary: NY.red,
    colorInfo: NY.red,
    colorText: NY.ink,
    colorTextSecondary: NY.muted,
    colorBorder: NY.lineStrong,
    colorBorderSecondary: NY.line,
    colorBgLayout: NY.paper,
    colorBgContainer: NY.card,
    colorSuccess: NY.green,
    colorError: NY.red,
    colorWarning: NY.amber,
    borderRadius: 10,
    fontSize: 13,
  },
}

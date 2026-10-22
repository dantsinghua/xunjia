<script setup lang="ts">
/** 债券详情弹窗:内部意向买卖卡 / 市场最优(按渠道切换) / 最近成交表 /
 *  历史YTM折线SVG / 盘中三线 / 右侧对手卡(点击跳监控)。还原设计稿 hasModal。 */
import { ref, watch } from 'vue'
import { api } from '../api'
import { STATUS_COLOR } from '../theme'
import type { BondRow, BondDetail } from '../lib/types'
import { useBodyScrollLock } from '../lib/useModal'

const props = defineProps<{ bond: BondRow | null }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'jump', cpId: string, bondName: string): void }>()
useBodyScrollLock(() => !!props.bond)

const detail = ref<BondDetail | null>(null)
const loadError = ref('')
let generation = 0
const channel = ref(0)
const range = ref(2) // 默认 1M
const RANGES = ['7D', '14D', '1M', '3M', '6M']

watch(() => props.bond, async (b) => {
  const request = ++generation
  detail.value = null
  loadError.value = ''
  channel.value = 0
  if (!b) return
  try {
    const r = await api.bond(b.code, b)
    if (request === generation) detail.value = r.data
  } catch (e) { if (request === generation) loadError.value = e instanceof Error ? e.message : '读取失败' }
}, { immediate: true })

function st(status: string) { return STATUS_COLOR[status] || STATUS_COLOR['待询价'] }
function tagStyle(t: string): [string, string] {
  const M: Record<string, [string, string]> = { 绿: ['rgba(46,125,79,.14)', '#1F6B3E'], 科创: ['rgba(30,95,180,.12)', '#1E5FB4'], 公募: ['rgba(198,141,88,.2)', '#7A5A2E'], 私募: ['rgba(110,98,88,.14)', '#5C5148'] }
  return M[t] || ['rgba(110,98,88,.14)', '#5C5148']
}
function dirColor(d: string) { return d === 'GVN' ? '#B01C24' : d === 'TKN' ? '#1F6B3E' : '#7A5A2E' }
</script>

<template>
  <Teleport to="body">
  <div v-if="bond" class="ov" @click="emit('close')">
    <div class="mdl pop-in bond-mdl" @click.stop>
      <!-- 头:固定,不进滚动条 -->
      <div class="mdl-hd">
        <div class="head">
          <div class="head-l">
            <h2 class="serif">{{ bond.name }}</h2>
            <span v-for="t in bond.tags" :key="t" class="tag" :style="{ background: tagStyle(t)[0], color: tagStyle(t)[1] }">{{ t }}</span>
            <span class="term">剩余期限 <b>{{ bond.term }}</b> · 中债估值 <b class="mono">{{ bond.val }}</b></span>
            <span class="status" :style="{ background: st(bond.status)[0], color: st(bond.status)[1] }">{{ bond.status }}</span>
          </div>
          <button class="x" @click="emit('close')"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18" /></svg></button>
        </div>
        <p class="tip">左侧为我方内部意向与市场最优报价,中间为成交与走势,右侧为机器人今日在聊的对手——点击对手卡片可直达聊天记录。</p>
      </div>

      <div class="mdl-bd">
      <div v-if="detail" class="grid">
        <!-- 左列 -->
        <div class="col">
          <!-- 内部意向 -->
          <div class="card">
            <div class="card-title">内部意向指令</div>
            <div class="two">
              <div class="side-card bid">
                <div class="side-head bid">买</div>
                <div class="side-body">
                  <div class="kv"><span>投资经理</span><b>{{ bond.buyMgr }}</b></div>
                  <div class="kv"><span>报 bid</span><b class="mono red">{{ bond.ib }}</b></div>
                  <div class="kv"><span>bid 量(万)</span><b class="mono">{{ bond.ibv }}</b></div>
                </div>
              </div>
              <div class="side-card ofr">
                <div class="side-head ofr">卖</div>
                <div class="side-body">
                  <div class="kv"><span>投资经理</span><b>{{ bond.sellMgr }}</b></div>
                  <div class="kv"><span>报 ofr</span><b class="mono green">{{ bond.io }}</b></div>
                  <div class="kv"><span>ofr 量(万)</span><b class="mono">{{ bond.iov }}</b></div>
                </div>
              </div>
            </div>
          </div>

          <!-- 市场最优 -->
          <div class="card">
            <div class="card-hd">
              <div class="card-title">当前市场最优报价</div>
              <div class="chans">
                <span v-for="(ch, i) in detail.channels" :key="i" class="chan" :class="{ on: channel === i }" @click="channel = i">{{ ch.label }}</span>
              </div>
            </div>
            <div v-if="detail.channels[channel]" class="two best">
              <div class="best-card bid">
                <div class="bl">最优 Bid</div>
                <div class="bv mono">{{ detail.channels[channel].bid }}</div>
                <div class="bm">{{ detail.channels[channel].bidVol }}(万) · {{ detail.channels[channel].bidSide }}</div>
              </div>
              <div class="best-card ofr">
                <div class="bl">最优 Ofr</div>
                <div class="bv mono">{{ detail.channels[channel].ofr }}</div>
                <div class="bm">{{ detail.channels[channel].ofrVol }}(万) · {{ detail.channels[channel].ofrSide }}</div>
              </div>
            </div>
          </div>

          <!-- 最近成交 -->
          <div class="card">
            <div class="card-hd"><div class="card-title">最近成交</div><span class="muted">今日 {{ detail.trades.length }} 笔</span></div>
            <table class="tr-table">
              <thead><tr><th>时间</th><th>渠道</th><th>方向</th><th class="r">成交价</th><th class="r">量(万)</th></tr></thead>
              <tbody>
                <tr v-for="(t, i) in detail.trades" :key="i">
                  <td class="mono muted">{{ t.t }}</td>
                  <td><span class="ch-pill">{{ t.ch }}</span></td>
                  <td><b :style="{ color: dirColor(t.dir) }">{{ t.dir }}</b></td>
                  <td class="r mono b">{{ t.p }}</td>
                  <td class="r mono muted">{{ t.v }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- 中列:图表 -->
        <div class="col">
          <div class="card">
            <div class="card-hd">
              <div class="card-title">历史成交价格 · YTM(%)</div>
              <div class="ranges"><span v-for="(rg, i) in RANGES" :key="i" class="rg" :class="{ on: range === i }" @click="range = i">{{ rg }}</span></div>
            </div>
            <p v-if="!detail.hist.avg && !detail.hist.zz" class="muted">暂无已接入的历史成交序列</p>
            <div class="legend"><span><i class="ls red" />平均成交</span><span><i class="ls gold dash" />中债估值</span></div>
            <svg v-if="detail.hist.avg || detail.hist.zz" viewBox="0 0 560 170" class="chart">
              <g stroke="rgba(176, 28, 36,.1)" stroke-width="1"><line x1="40" y1="20" x2="550" y2="20" /><line x1="40" y1="60" x2="550" y2="60" /><line x1="40" y1="100" x2="550" y2="100" /><line x1="40" y1="140" x2="550" y2="140" /></g>
              <g font-family="var(--font-ui)" font-size="10" fill="#A89C90"><text x="4" y="24">{{ detail.hist.max }}</text><text x="4" y="144">{{ detail.hist.min }}</text><text x="40" y="164">{{ detail.hist.d0 }}</text><text x="285" y="164">{{ detail.hist.d1 }}</text><text x="500" y="164">{{ detail.hist.d2 }}</text></g>
              <polyline :points="detail.hist.zz" fill="none" stroke="#C68D58" stroke-width="1.6" stroke-dasharray="4 4" />
              <polyline :points="detail.hist.avg" fill="none" stroke="#C2333B" stroke-width="2.2" stroke-linejoin="round" style="filter:drop-shadow(0 0 6px rgba(176, 28, 36,.45))" />
            </svg>
          </div>
          <div class="card">
            <div class="card-title">日内报价走势 · YTM(%)</div>
            <div class="legend"><span><i class="ls red" />bid</span><span><i class="ls green" />ofr</span><span><i class="ls gold dash" />中债估值</span></div>
            <svg v-if="detail.intra.bid || detail.intra.ofr" viewBox="0 0 560 170" class="chart">
              <g stroke="rgba(176, 28, 36,.1)" stroke-width="1"><line x1="40" y1="20" x2="550" y2="20" /><line x1="40" y1="60" x2="550" y2="60" /><line x1="40" y1="100" x2="550" y2="100" /><line x1="40" y1="140" x2="550" y2="140" /></g>
              <g font-family="var(--font-ui)" font-size="10" fill="#A89C90"><text x="4" y="24">{{ detail.intra.max }}</text><text x="4" y="144">{{ detail.intra.min }}</text><text x="40" y="164">09:00</text><text x="170" y="164">10:30</text><text x="300" y="164">13:30</text><text x="430" y="164">14:30</text><text x="525" y="164">15:00</text></g>
              <polyline :points="detail.intra.zz" fill="none" stroke="#C68D58" stroke-width="1.6" stroke-dasharray="4 4" />
              <polyline :points="detail.intra.bid" fill="none" stroke="#C2333B" stroke-width="2.2" stroke-linejoin="round" style="filter:drop-shadow(0 0 6px rgba(176, 28, 36,.45))" />
              <polyline :points="detail.intra.ofr" fill="none" stroke="#2E8B57" stroke-width="2.2" stroke-linejoin="round" style="filter:drop-shadow(0 0 6px rgba(46,125,79,.4))" />
            </svg>
          </div>
        </div>

        <!-- 右列:在聊对手 -->
        <div class="card cps-col">
          <div class="card-hd"><div class="card-title">今日机器人在聊对手</div><span class="muted">{{ detail.modalCps.length }} 个</span></div>
          <p class="tip sm">点击卡片跳转至机器人监控查看完整聊天记录</p>
          <div v-if="!detail.modalCps.length" class="empty-cp">暂无机器人在聊该债券<br><span>状态变为「询价中」后自动出现</span></div>
          <div v-for="cp in detail.modalCps" :key="cp.id" class="cp-card" @click="emit('jump', cp.id, bond!.code)">
            <div class="cp-top"><span class="qd">企点</span><b>{{ cp.name }}</b><span class="dot" :style="{ background: st(cp.stage)[1], boxShadow: '0 0 8px ' + st(cp.stage)[1] }" /></div>
            <div class="cp-mid"><span class="stage" :style="{ background: st(cp.stage)[0], color: st(cp.stage)[1] }">{{ cp.stage }}</span></div>
            <div class="cp-bot"><span>与底价价差 <b class="mono red">{{ cp.spread }}</b></span><span class="mono muted">{{ cp.updated }}</span></div>
          </div>
        </div>
      </div>
      <div v-else class="loading">{{ loadError || '加载中…' }}</div>
      </div>
    </div>
  </div>
  </Teleport>
</template>

<style scoped>
/* 遮罩/骨架/滚动区用 styles.css 的 .ov / .mdl / .mdl-hd / .mdl-bd,此处只定宽 */
.bond-mdl { max-width: 1360px; }
.head { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.head-l { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.head h2 { margin: 0; font-weight: 900; font-size: 24px; color: #B01C24; }
.code { font-size: 13px; color: #8A7F76; }
.tag { padding: 2px 8px; border-radius: 5px; font-size: 11px; font-weight: 700; }
.term { font-size: 12.5px; color: #6E6258; }
.status { padding: 4px 11px; border-radius: 999px; font-size: 12px; font-weight: 600; }
.x { cursor: pointer; width: 34px; height: 34px; border-radius: 50%; border: 1px solid rgba(176, 28, 36,.25); background: rgba(255,255,255,.7); color: #B01C24; display: grid; place-items: center; }
.x:hover { background: rgba(176, 28, 36,.1); }
.tip { margin: 6px 0 0; font-size: 12px; color: #A89C90; }
.tip.sm { font-size: 11.5px; margin: 0; }

/* 版面自适应:宽屏三列,中屏两列,窄屏单列。minmax 的 0 下限防止图表把列撑破 */
.grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 18px; align-items: start; }
@media (max-width: 1180px) { .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 760px) { .grid { grid-template-columns: minmax(0, 1fr); } }
.col { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.card { border-radius: 16px; padding: 16px; background: rgba(255,255,255,.7); border: 1px solid rgba(176, 28, 36,.12); box-shadow: 0 6px 20px rgba(43,35,32,.05); }
.card-title { font-size: 12px; font-weight: 700; letter-spacing: 1.5px; color: #8A7F76; }
.card-hd { display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
.muted { color: #A89C90; font-size: 11.5px; }

.two { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 10px; }
.side-card { border-radius: 10px; overflow: hidden; border: 1px solid rgba(176, 28, 36,.2); }
.side-card.ofr { border-color: rgba(46,125,79,.3); }
.side-head { color: #fff; text-align: center; font-weight: 700; padding: 6px; font-size: 13px; letter-spacing: 3px; }
.side-head.bid { background: linear-gradient(135deg, #C2333B, #8E1620); }
.side-head.ofr { background: linear-gradient(135deg, #2E8B57, #1F6B3E); }
.side-body { padding: 10px 8px; font-size: 12px; display: grid; gap: 6px; background: rgba(176, 28, 36,.04); }
.side-card.ofr .side-body { background: rgba(46,125,79,.05); }
.kv { display: flex; justify-content: space-between; }
.kv span { color: #A89C90; }
.red { color: #B01C24; }
.green { color: #1F6B3E; }

.chans { display: flex; gap: 4px; flex-wrap: wrap; }
.chan { cursor: pointer; padding: 4px 9px; border-radius: 7px; font-size: 11.5px; font-weight: 600; background: rgba(255,255,255,.6); color: #5C5148; border: 1px solid rgba(176, 28, 36,.16); transition: all .18s; }
.chan.on { background: linear-gradient(135deg, #C2333B, #8E1620); color: #fff; border-color: transparent; }
.best { margin-top: 12px; }
.best-card { border-radius: 12px; padding: 18px 12px; text-align: center; color: #fff; }
.best-card.bid { background: linear-gradient(160deg, #C2333B, #7C231F); box-shadow: 0 0 22px rgba(176, 28, 36,.35); }
.best-card.ofr { background: linear-gradient(160deg, #2E8B57, #1A5C36); box-shadow: 0 0 22px rgba(46,125,79,.3); }
.bl { font-size: 12px; opacity: .85; }
.bv { font-weight: 700; font-size: 30px; margin-top: 4px; }
.bm { font-size: 12px; opacity: .85; margin-top: 2px; }

.tr-table { width: 100%; border-collapse: collapse; font-size: 12.5px; margin-top: 8px; }
.tr-table th { text-align: left; padding: 6px 4px; font-size: 11px; color: #A89C90; font-weight: 600; border-bottom: 1px solid rgba(176, 28, 36,.12); }
.tr-table td { padding: 7px 4px; border-bottom: 1px solid rgba(176, 28, 36,.06); }
.tr-table .r { text-align: right; }
.tr-table .b { font-weight: 700; }
.ch-pill { padding: 1px 7px; border-radius: 5px; font-size: 11px; background: rgba(176, 28, 36,.08); color: #B01C24; }

.legend { display: flex; gap: 14px; font-size: 11.5px; color: #6E6258; margin-top: 8px; }
.ls { display: inline-block; width: 10px; height: 3px; border-radius: 2px; vertical-align: middle; margin-right: 5px; }
.ls.red { background: #C2333B; }
.ls.green { background: #2E8B57; }
.ls.gold { background: #C68D58; }
.ls.dash { height: 0; border-top: 2px dashed #C68D58; background: none; }
.chart { width: 100%; height: 190px; margin-top: 6px; overflow: visible; }
.ranges, .chans { display: flex; gap: 4px; }
.rg { cursor: pointer; padding: 3px 9px; border-radius: 6px; font-size: 11.5px; font-weight: 600; background: transparent; color: #8A7F76; border: 1px solid rgba(176, 28, 36,.12); }
.rg.on { background: rgba(176, 28, 36,.1); color: #B01C24; border-color: rgba(176, 28, 36,.4); }

.cps-col { display: flex; flex-direction: column; gap: 10px; }
.empty-cp { padding: 26px 12px; text-align: center; font-size: 12.5px; color: #A89C90; border: 1px dashed rgba(176, 28, 36,.2); border-radius: 12px; }
.empty-cp span { font-size: 11.5px; }
.cp-card { cursor: pointer; border-radius: 12px; padding: 12px 13px; background: rgba(255,255,255,.8); border: 1px solid rgba(176, 28, 36,.14); transition: all .22s; }
.cp-card:hover { border-color: rgba(176, 28, 36,.55); box-shadow: 0 0 18px rgba(176, 28, 36,.22); transform: translateY(-2px); }
.cp-top { display: flex; align-items: center; gap: 8px; }
.cp-top b { font-size: 13.5px; }
.qd { padding: 2px 7px; border-radius: 6px; font-size: 10.5px; font-weight: 700; background: rgba(46,125,79,.1); color: #1F6B3E; }
.cp-top .dot { margin-left: auto; width: 9px; height: 9px; border-radius: 50%; }
.cp-mid { margin-top: 8px; }
.stage { padding: 2px 9px; border-radius: 999px; font-size: 11px; font-weight: 600; }
.cp-bot { display: flex; justify-content: space-between; margin-top: 8px; font-size: 11.5px; color: #6E6258; }
.loading { padding: 60px; text-align: center; color: #A89C90; }
</style>

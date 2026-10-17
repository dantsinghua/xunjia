/** 弹窗打开时锁住背景滚动。
 *  五个弹窗(债券详情/询价策略/链路追踪/询价明细/导出记录)共用,叠加时按引用计数,
 *  最后一个关掉才解锁;隐藏滚动条会让内容右移,故同时补等宽 padding 防抖动。 */
import { onUnmounted, watch } from 'vue'

let openCount = 0

function lock() {
  if (openCount++ > 0) return
  const sbw = window.innerWidth - document.documentElement.clientWidth
  document.body.style.setProperty('--sbw', sbw + 'px')
  document.body.classList.add('modal-open')
}

function unlock() {
  if (openCount === 0 || --openCount > 0) return
  document.body.classList.remove('modal-open')
  document.body.style.removeProperty('--sbw')
}

export function useBodyScrollLock(isOpen: () => boolean) {
  let held = false
  watch(isOpen, (v) => {
    if (v && !held) { lock(); held = true }
    else if (!v && held) { unlock(); held = false }
  }, { immediate: true })
  onUnmounted(() => { if (held) { unlock(); held = false } })
}

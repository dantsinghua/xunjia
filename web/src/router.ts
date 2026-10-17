import { createRouter, createWebHashHistory } from 'vue-router'
import Quote from './pages/Quote.vue'
import Monitor from './pages/Monitor.vue'
import Chat from './pages/Chat.vue'
import Link from './pages/Link.vue'

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/quote' },
    { path: '/quote', component: Quote, meta: { title: '意向询价' } },
    { path: '/monitor', component: Monitor, meta: { title: '机器人监控' } },
    { path: '/chat', component: Chat, meta: { title: '智能问答' } },
    { path: '/link', component: Link, meta: { title: '采集通道' } },
  ],
})

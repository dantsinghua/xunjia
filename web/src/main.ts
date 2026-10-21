import { createApp } from 'vue'
import Antd from 'ant-design-vue'
import App from './App.vue'
import { router } from './router'
import './styles.css'
import { startBuildVersionWatch } from './lib/buildVersion'

createApp(App).use(Antd).use(router).mount('#app')

declare const __XUNJIA_BUILD_VERSION__: string
if (__XUNJIA_BUILD_VERSION__ !== 'development') startBuildVersionWatch(__XUNJIA_BUILD_VERSION__)

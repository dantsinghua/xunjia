import { createApp } from 'vue'
import Antd from 'ant-design-vue'
import App from './App.vue'
import { router } from './router'
import './styles.css'

createApp(App).use(Antd).use(router).mount('#app')

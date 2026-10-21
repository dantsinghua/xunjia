<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref } from "vue"
import { requestJson } from "../lib/http"
import { createSsoProof, type SsoProof } from "../lib/sso"

defineProps<{ busy: boolean; error: string; portalFailure: boolean }>()
const emit = defineEmits<{ login: [proof: SsoProof, path: string]; retry: []; reset: [] }>()
const booting = ref(true), devLogin = ref(false), configError = ref(""), formError = ref("")
const userName = ref(""), nickName = ref(""), secret = ref(""), menu = ref("/quote")
const menus = [{ path: "/quote", name: "意向询价" }, { path: "/monitor", name: "机器人监控" }, { path: "/chat", name: "智能问答" }, { path: "/link", name: "采集通道" }]
async function loadConfig() {
  booting.value = true; devLogin.value = false; configError.value = ""
  try {
    const cfg = await requestJson<{ok: boolean; dev_login: boolean; dev_user_name?: string; dev_nick_name?: string}>("/api/auth/sso-config")
    if (cfg.ok !== true || typeof cfg.dev_login !== "boolean") throw new Error("登录配置无效，请重试")
    devLogin.value = cfg.dev_login === true
    if (!userName.value) userName.value = cfg.dev_user_name || ""
    if (!nickName.value) nickName.value = cfg.dev_nick_name || ""
  } catch (e) { configError.value = e instanceof Error ? e.message : "登录配置暂不可用" }
  finally { booting.value = false }
}
function submit() {
  formError.value = ""
  try {
    const proof = createSsoProof(userName.value, nickName.value, secret.value)
    secret.value = ""
    emit("login", proof, menus.some(m => m.path === menu.value) ? menu.value : "/quote")
  } catch (e) { formError.value = e instanceof Error ? e.message : "登录参数无效" }
}
function retry() { void loadConfig(); emit("retry") }
onMounted(loadConfig)
onBeforeUnmount(() => { secret.value = "" })
</script>

<template>
  <section class="glass login-gate" data-testid="xj-login-gate" role="status">
    <h2>询价工作台登录</h2>
    <p v-if="busy">正在验证身份…</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="booting">正在读取登录配置…</p>
    <p v-if="configError" class="error" role="alert">{{ configError }}</p>
    <template v-if="portalFailure">
      <p>门户登录参数无效或已过期，请从南银门户重新进入。不会自动切换为测试身份。</p>
      <button v-if="devLogin" type="button" class="btn-primary" :disabled="busy" @click="emit(&quot;reset&quot;)">返回联调登录</button>
    </template>
    <form v-else-if="!booting && devLogin" data-testid="xj-dev-login" @submit.prevent="submit">
      <p>联调测试：与南银存款项目一致，填写账号、显示名和约定密钥后进入。正式环境由门户带签跳转。</p>
      <label for="xj-dev-user">userName（账号 / 用户ID）</label>
      <input id="xj-dev-user" v-model="userName" name="userName" maxlength="160" autocomplete="username" :disabled="busy" required />
      <label for="xj-dev-name">nickName（顶栏显示名）</label>
      <input id="xj-dev-name" v-model="nickName" name="nickName" maxlength="160" autocomplete="nickname" :disabled="busy" required />
      <label for="xj-dev-secret">约定密钥（仅用于本次签名）</label>
      <input id="xj-dev-secret" v-model="secret" name="secret" type="password" maxlength="4096" autocomplete="off" :disabled="busy" required />
      <p class="hint">密钥与本服务的 XUNJIA_SSO_SECRET 一致；不是STS／OSS密钥，不会保存到浏览器。</p>
      <label for="xj-dev-menu">进入菜单</label>
      <select id="xj-dev-menu" v-model="menu" :disabled="busy"><option v-for="m in menus" :key="m.path" :value="m.path">{{ m.name }}</option></select>
      <p v-if="formError" class="error" role="alert">{{ formError }}</p>
      <button class="btn-primary" type="submit" :disabled="busy">{{ busy ? "正在登录…" : "登录并进入" }}</button>
    </form>
    <p v-else-if="!booting">请从已授权的门户进入询价工作台。本环境未开启联调密钥登录。</p>
    <button v-if="!portalFailure" type="button" class="retry" :disabled="busy || booting" @click="retry">重新验证</button>
  </section>
</template>

<style scoped>
.login-gate { margin:40px auto; padding:32px; max-width:560px; border-radius:16px; }
form { display:grid; gap:10px; }
p { line-height:1.65; }
label { font-weight:600; margin-top:8px; }
input, select { box-sizing:border-box; width:100%; padding:11px 12px; border:1px solid #dacfc6; border-radius:8px; background:#fff; color:#2B2320; font:inherit; }
input:focus, select:focus { outline:2px solid #b01c24; outline-offset:1px; }
.error { color:#a22121; overflow-wrap:anywhere; }
.hint { font-size:12px; color:#6e6258; margin:0; }
button { cursor:pointer; padding:10px 16px; border-radius:8px; font:inherit; }
button:disabled { opacity:.6; cursor:wait; }
.retry { margin-top:16px; border:1px solid #dacfc6; border-radius:8px; background:transparent; }
</style>

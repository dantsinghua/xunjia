// Compile the actual Quote setup and exercise event handlers with synthetic HTTP.
import { build } from 'esbuild'
import { parse, compileScript } from '@vue/compiler-sfc'
import { readFile, mkdtemp } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { tmpdir } from 'node:os'
import { pathToFileURL } from 'node:url'

const folder = await mkdtemp(join(tmpdir(), 'xj-quote-acceptance-'))
const outfile = join(folder, 'probe.mjs')
await build({
  stdin: { contents: `
    import Quote from './src/pages/Quote.vue';
    import Rfq from './src/components/RfqAssignModal.vue';
    import { imLinks } from './src/lib/imLink';
    import { effectScope } from 'vue';
    const results = {};
    for (const [handler, status] of [['onRfqSent','待回执'],['onRfqPending','待回执'],['onRfqFailed','询价中']]) {
      const requests = [];
      globalThis.fetch = async url => {
        requests.push(String(url));
        const data = String(url).includes('/rfq/status') ? {items:[{code:'000100.IB',status}]} : {items:[]};
        return new Response(JSON.stringify({ok:true,data}));
      };
      const scope = effectScope();
      const vm = scope.run(() => Quote.setup({}, {expose(){},emit(){}}));
      vm.bonds.value = [{code:'000100.IB', name:'合成债券', status:'待询价',tags:[],
        obs:'',oos:'',ib:'—',io:'—',ob:'—',oo:'—',ibv:'—',iov:'—',obv:'—',oov:'—'}];
      await vm[handler](['000100.IB']);
      results[handler] = {status:vm.rows.value[0].status, requests};
      globalThis.fetch = async () => {throw new Error('synthetic read failure')};
      await vm[handler](['000100.IB']);
      results[handler].afterReadFailure = vm.rows.value[0].status;
      scope.stop();
    }
    results.modal = {};
    globalThis.window = {innerWidth:1440};
    globalThis.document = {documentElement:{clientWidth:1440},body:{
      style:{setProperty(){},removeProperty(){}},classList:{add(){},remove(){}}}};
    imLinks.value = [{id:'test-link',active:true,tested:true,mode:'oss',catalog_stale:false,
      account_uid:'910001',workbench_id:'test-wb',contacts:[],form:{}}];
    for (const commandStatus of ['queued','sent','failed']) {
      const calls = [], events = [];
      globalThis.fetch = async (url, options) => {
        calls.push({url:String(url),body:JSON.parse(options.body)});
        const data = String(url).endsWith('/preview')
          ? {batch_id:'batch-test',version:1,status:'DRAFT',items:[],commands_preview:[{peer_id:'peer',text:'服务器首句'}]}
          : {batch_id:'batch-test',commands:[{command_id:'cmd-test',peer_id:'peer',status:commandStatus}]};
        return new Response(JSON.stringify({ok:true,data}));
      };
      const scope = effectScope();
      const vm = scope.run(() => Rfq.setup({open:true,bonds:[]}, {expose(){},emit(event){events.push(event)}}));
      vm.drafts.value = [{code:'000100.IB',fields:{name:'合成债'},text:'参考行情',uins:['peer'],our_side:'BUY',
        volume_wan:'1000',opening_yield_pct:'2.50',limit_yield_pct:'2.40',step_bp:'2'}];
      await vm.confirm();
      const beforePreview = calls.length;
      await vm.requestPreview();
      await vm.confirm();
      results.modal[commandStatus] = {beforePreview,events,calls};
      scope.stop();
    }
    console.log(JSON.stringify(results));
  `, resolveDir: resolve('.'), loader: 'ts' },
  bundle: true, platform: 'node', format: 'esm', outfile,
  plugins: [{ name: 'acceptance-vue', setup(builder) {
    builder.onResolve({filter:/^(ant-design-vue|vue-router)$/}, args => ({path:args.path,namespace:'stub'}));
    builder.onLoad({filter:/.*/,namespace:'stub'}, args => ({contents:args.path==='ant-design-vue'
      ? 'export const App={useApp:()=>({message:{success(){},error(){},warning(){},info(){}}})}'
      : 'export const useRoute=()=>({query:{}});export const useRouter=()=>({push(){}})'}));
    builder.onLoad({filter:/\.vue$/}, async args => {
      const {descriptor}=parse(await readFile(args.path,'utf8'));
      return {contents:compileScript(descriptor,{id:args.path}).content,loader:'ts',resolveDir:dirname(args.path)};
    });
  }}],
});
await import(pathToFileURL(outfile).href);

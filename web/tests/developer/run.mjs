import { build } from 'esbuild'
import { parse, compileScript } from '@vue/compiler-sfc'
import { readFile } from 'node:fs/promises'
import { dirname } from 'node:path'
import { mkdtemp, readdir } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'
const dir=await mkdtemp(join(tmpdir(),'xj-web-tests-'))
await build({entryPoints:process.argv.slice(2).length?process.argv.slice(2):['tests/developer/api.test.ts','tests/developer/components.test.ts','tests/developer/quoteAuth.test.ts','tests/developer/rfqPreview.test.ts','tests/developer/rfqDirectRecovery.test.ts','tests/developer/accountContextRace.test.ts','tests/developer/serverManagedAuth.test.ts','tests/developer/tradeExport.test.ts','tests/developer/negotiationV22.test.ts'],bundle:true,platform:'node',format:'esm',outdir:dir,plugins:[{name:'sfc-test',setup(b){
 b.onResolve({filter:/^(ant-design-vue|vue-router)$/},a=>({path:a.path,namespace:'stub'}))
 b.onLoad({filter:/.*/,namespace:'stub'},a=>({contents:a.path==='ant-design-vue'?`export const App={useApp:()=>({message:{success(){},error(){},warning(){},info(){}}})}`:`export const useRoute=()=>({query:{}});export const useRouter=()=>({push(){}})`}))
 b.onLoad({filter:/\.vue$/},async a=>{const {descriptor}=parse(await readFile(a.path,'utf8'));const script=compileScript(descriptor,{id:a.path});return {contents:script.content,loader:'ts',resolveDir:dirname(a.path)}})
} }]})
const result=spawnSync(process.execPath,['--test',...(await readdir(dir)).map(f=>join(dir,f))],{stdio:'inherit'})
process.exitCode=result.status??1

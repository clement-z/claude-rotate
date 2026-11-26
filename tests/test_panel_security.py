"""Render the actual panel JavaScript with a tiny DOM sink harness (Node optional)."""
import ast
import json
import shutil
import subprocess
from pathlib import Path


def main():
    node = shutil.which("node")
    if not node:
        print("SKIP panel JavaScript execution: Node.js is not installed")
        return
    tree = ast.parse((Path(__file__).resolve().parent.parent / "rotator.py").read_text())
    panel = next(ast.literal_eval(n.value) for n in tree.body
                 if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PANEL_HTML" for t in n.targets))
    script = panel.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = r'''
const vm = require('node:vm');
const assert = require('node:assert/strict');
const script = JSON.parse(process.argv[1]);
const value = '<mark data-audit="marker">&\'"</mark>';
const elements = {}, urls = [], replaced = [];
const usage = {requests:1,input_tokens:1,output_tokens:1,cache_read:0,cache_write:0,cost_usd:0};
const stats = {devices:{[value]:{last_model:value,last_account:value,key_suffix:value}},alerts:[{level:'warn',text:value}],by_device:{[value]:usage},by_model:{[value]:usage},by_account:{[value]:usage},periods:{since:value,'7d':{device:{[value]:usage},model:{[value]:usage}},'30d':{device:{[value]:usage},model:{[value]:usage}},all:{device:{[value]:usage,[value+'-gone']:usage},model:{[value]:usage}},trend:{[value]:[0,1,2]}},billing:{[value]:{plan:value,price:200,renews:value,periods:[{start:value,end:value,days:31,elapsed:20,covered:18,plans:[value],price:180.5,used:70,peak:80,pool_peak:60,limited_h:1,blocked_h:0.5,usage,whatif:[{plan:value,price:-100,pool_peak:120}]}]}}};
const status = {accounts:{[value]:{priority:100},[value+'2']:{priority:100}},active_account:value,pinned:value,strategy:value,switch_threshold:.8,switch_threshold_7d:.98,recent_events:[{ts:value,from:value,to:value,reason:value}],weekly:{series:[[0,0,100],[3600,10,100]],projection:[[3600,10,100],[7200,100,100]],rate_per_day:5,eta:7200,back:9000,back_account:value,capacity:200},
  providers:{[value]:{kind:'openai-siwc',state:value,reason:value,
                      quotas:{[value]:{primary:{used_percent:95,window_minutes:value,resets_at:value},secondary:{used_percent:value,window_minutes:300,resets_at:0},plan:value,route:value,seen_at:value},
                              imagegen_premium:{primary:{used_percent:40,window_minutes:10080,resets_at:9e9},secondary:null,plan:value,route:'images',seen_at:0}},
                      images:{state:value,reason:value,unsaved:value,last_error:value,access_token_min:value,last_answer:{status:value,at:value}}},
             [value+'-bare']:{kind:'openai-siwc',state:'ok',quotas:{}},
             [value+'-static']:{kind:'static-bearer',state:'ok',reason:value}}};
const context = vm.createContext({URL,URLSearchParams,location:{search:'?key=fake-viewer-key',href:'http://proxy/rotate/panel?key=fake-viewer-key'},history:{replaceState:(_a,_b,url)=>replaced.push(url)},document:{getElementById:id=>elements[id]??=( {innerHTML:'',textContent:''} )},setInterval:()=>{},fetch:async(url,opts)=>{urls.push({url,opts});return {ok:true,json:async()=>url.includes('/status')?status:stats}}});
elements.confirm={showModal(){this.open=true},close(){this.open=false}};
(async()=>{
  vm.runInContext(script,context,{timeout:1000});
  await vm.runInContext('refresh()',context,{timeout:1000});
  assert(replaced.every(url=>!url.includes('key=')));
  assert(urls.every(({url,opts})=>!url.includes('key=')&&opts.headers.Authorization==='Bearer fake-viewer-key'));
  assert(elements.weekly.innerHTML.includes('class="hit"'),'shortfall band not drawn');
  assert(elements.devices.innerHTML.includes('removed'),'removed device not listed');
  for(const id of ['pills','accounts','oai','weekly','billing','alerts','devices','models','by-account','events']){
    assert(!elements[id].innerHTML.includes('<mark'),id+' contains raw markup');
    assert(elements[id].innerHTML.includes('&lt;mark'),id+' lost escaped text');
  }
  const oai=elements.oai.innerHTML;
  assert.equal(oai.split('ChatGPT plan').length-1,2,'one quota card per openai-siwc provider, none for static-bearer');
  assert(!oai.includes(value+'-static')&&!oai.includes('&lt;mark data-audit=&quot;marker&quot;&gt;&amp;&#39;&quot;&lt;/mark&gt;-static'),'static-bearer provider got a quota card');
  assert(oai.includes('images ')&&oai.includes('title="&lt;mark'),'images badge or its escaped title missing');
  assert(oai.includes('>7d<')&&oai.includes('images · ')&&oai.includes('model · read only with an images login'),'quota rows or the empty card missing');
  assert(elements.pills.innerHTML.includes('>Rotate<')&&!elements.pills.innerHTML.includes('disabled'),'rotate button missing');
  assert(elements.accounts.innerHTML.includes('Resume auto')&&elements.accounts.innerHTML.includes('>Select<'),'account buttons missing');
  elements.accounts.onclick({target:{closest:()=>({dataset:{sw:value+'2'}})}});
  assert(elements.confirm.open,'confirmation not shown');
  assert(!elements['cf-text'].innerHTML.includes('<mark')&&elements['cf-text'].innerHTML.includes('&lt;mark'),'confirmation not escaped');
  await elements['cf-yes'].onclick();
  const post=urls.find(u=>u.url==='/rotate/switch');
  assert(post&&post.opts.method==='POST'&&JSON.parse(post.opts.body).account===value+'2'
    &&post.opts.headers.Authorization==='Bearer fake-viewer-key'&&post.opts.headers['Content-Type']==='application/json','switch request');
  assert(!elements.confirm.open,'confirmation stays open after a switch');
  console.log('panel JavaScript security checks passed');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    subprocess.run([node, "-e", harness, json.dumps(script)], check=True)


if __name__ == "__main__":
    main()

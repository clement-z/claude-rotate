"""Render the actual panel JavaScript with a tiny DOM sink harness (Node optional)."""
import ast
import json
import re
import shutil
import subprocess
from pathlib import Path


def main():
    tree = ast.parse((Path(__file__).resolve().parent.parent / "rotator.py").read_text())
    panel = next(ast.literal_eval(n.value) for n in tree.body
                 if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PANEL_HTML" for t in n.targets))
    assert "General weekly quota runs out in" in panel
    assert 'id="quota-rate-empty" class="muted">Computing…' in panel
    assert "Fable-only use of the Fable allowance" in panel and "to show its value" in panel
    assert re.search(r'<div id="weekly"[^>]*>[^<]*</div>\s*<div class="tip" id="tip">', panel), \
        "the tooltip must be outside the chart so its taps do not reach the chart"
    node = shutil.which("node")
    if not node:
        print("SKIP panel JavaScript execution: Node.js is not installed")
        return
    script = panel.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = r'''
const vm = require('node:vm');
const assert = require('node:assert/strict');
const script = JSON.parse(process.argv[1]);
const value = '<mark data-audit="marker">&\'"</mark>';
const elements = {}, urls = [], replaced = [];
const usage = {requests:1,input_tokens:1,output_tokens:1,cache_read:0,cache_write:0,cost_usd:0};
const D=86400, now=1_800_000_000, start=now-14*D, reset=now-5*D;
const receipt=Math.floor(Date.now()/1000);
const stats = {devices:{[value]:{last_model:value,last_account:value,key_suffix:value}},alerts:[{level:'warn',text:value}],by_device:{[value]:usage},by_model:{[value]:usage},by_account:{[value]:usage},periods:{since:value,'7d':{device:{[value]:usage},model:{[value]:usage}},'30d':{device:{[value]:usage},model:{[value]:usage}},all:{device:{[value]:usage,[value+'-gone']:usage},model:{[value]:usage}},trend:{[value]:[0,1,2]}},billing:{[value]:{plan:value,price:200,renews:value,periods:[{start:value,end:value,days:31,elapsed:20,covered:18,plans:[value],price:180.5,used:70,peak:80,pool_peak:60,limited_h:1,blocked_h:0.5,usage,whatif:[{plan:value,price:-100,pool_peak:120}]}]}}};
const status = {accounts:{[value]:{priority:100},[value+'2']:{priority:100}},active_account:value,pinned:value,strategy:value,switch_threshold:.8,switch_threshold_7d:.98,recent_events:[{ts:value,from:value,to:value,reason:value}],weekly:{series:[[now-2*D,0,100],[now,10,100]],projection:[[now,10,100],[now+2*D,100,100]],rate_per_day:5,eta:now+2*D,back:now+3*D,back_account:value,capacity:200},
  providers:{[value]:{kind:'openai-siwc',state:value,reason:value,
                      quotas:{[value]:{primary:{used_percent:95,window_minutes:value,resets_at:value},secondary:{used_percent:value,window_minutes:300,resets_at:0},plan:value,route:value,seen_at:value},
                              imagegen_premium:{primary:{used_percent:40,window_minutes:10080,resets_at:9e9,seen_at:receipt-7200},secondary:null,plan:value,route:'images',seen_at:receipt}},
                      images:{state:value,reason:value,unsaved:value,last_error:value,access_token_min:value,last_answer:{status:value,at:value}}},
             [value+'-bare']:{kind:'openai-siwc',state:'ok',quotas:{}},
             [value+'-static']:{kind:'static-bearer',state:'ok',reason:value}}};
status.active_by_family={default:value,fable:value+'2'};
status.manual_by_family={default:value,fable:null};
status.pin_targets={[value]:true,[value+'2']:true,[value+'3']:false};
status.device_pins={[value]:{account_pin:{account:value,mode:'strict'},source:'config'}};
Object.assign(status.accounts[value],{last_seen:receipt,eligible_by_family:{default:true,fable:false},claims:{
  '5h':{label:'5h',families:['default','fable'],effective_utilization:null,freshness:'awaiting_reading',reset:receipt-D},
  '7d':{label:'7d',families:['default','fable'],effective_utilization:.3,threshold:.98,freshness:'current',status:'allowed',reset:receipt+D,seen_at:receipt-600},
  '7d_oi':{label:'Fable 7d',families:['fable'],effective_utilization:1,threshold:.98,freshness:'current',status:'rejected',block_reason:'rejected',reset:receipt+2*D,seen_at:receipt-720},
  [value]:{families:['default'],effective_utilization:.2,freshness:'current',status:'limited',reset:receipt+D}
}});
status.accounts[value+'2'].eligible_by_family={default:true,fable:true};
const quotaHistory={as_of:now,providers:{[value]:[
  {limit:value,minutes:10080,points:[
    [reset-850,10,reset,'usage'], [reset-550,20,reset+30,'usage'],
    [reset-250,25,reset+55,'usage'], [reset-100,26,reset+85,'usage'],
    [reset+100,27,reset+85,'usage'], [reset+400,1,reset+6*D,'usage'],
    [reset+700,10,reset+6*D,'usage'], [reset+1300,15,reset+6*D,'usage'],
    [reset+1600,14,reset+6*D,'usage'], [reset+1900,16,null,'usage']]},
  {limit:'imagegen_premium',minutes:1440,points:[[now-9*D,8,null,'images'],[now-D,0,null,'images']]},
  {limit:'inference',minutes:300,points:[[now-2*D,12,now+9*D,'inference']]}
]}};
class Element {
  constructor(){this._html='';this.children=[];this.style={};this.clientWidth=360;this.scrollLeft=0;
    this.offsetWidth=190;this.textContent='';this.nodes={};}
  get innerHTML(){return this._html}
  set innerHTML(html){
    this._html=html; this.nodes={}; this.children=[];
    const scrollTag='id="wk-scroll"';
    if(html.includes(scrollTag)){
      const sc=this.nodes['#wk-scroll']=new Element();
      sc.left=48; sc.clientWidth=312; sc.scrollWidth=350;
      sc.innerHTML=html.slice(html.indexOf(scrollTag)+scrollTag.length);
    }else if(html.includes('class="quota-chart"')){
      this.nodes['.quota-chart']=new Element();
      this.nodes['.tip']=new Element();
      this.nodes['.tip'].parentNode=this;
    }else if(html.includes('class="crosshair"')){
      this.nodes['.crosshair']=new Element();
      const svg=this.nodes.svg=new Element();
      svg.getBoundingClientRect=()=>({left:(this.left||0)-this.scrollLeft,top:0});
    }
  }
  getBoundingClientRect(){const left=this.left||0;return {left,top:0,width:this.clientWidth,right:left+this.clientWidth}}
  querySelector(k){return this.nodes[k]}
  setAttribute(k,v){this[k]=v}
  append(...children){this.children.push(...children)}
  prepend(...children){this.children.unshift(...children)}
  replaceChildren(){this.children=[]}
  showModal(){this.open=true}
  close(){this.open=false}
}
const html=e=>e.innerHTML+e.children.map(html).join('');
const allText=e=>e.textContent+e.children.map(allText).join(' ');
const document = {
  getElementById:id=>id==='wk-scroll'?elements.weekly?.querySelector('#wk-scroll'):elements[id]??=new Element(),
  createElement:()=>new Element(),
};
const timers=new Map();
let historyOK=true;
let quotaRateOK=true, quotaRate={status:'computing',computed_at:null,summary:[],fits:[]};
const schedule=(f,ms)=>timers.set(ms,f);
const context = vm.createContext({URL,URLSearchParams,location:{search:'?key=fake-viewer-key',href:'http://proxy/rotate/panel?key=fake-viewer-key'},history:{replaceState:(_a,_b,url)=>replaced.push(url)},document,window:{addEventListener(){}},setInterval:schedule,setTimeout:schedule,fetch:async(url,opts)=>{
  urls.push({url,opts});
  if(url==='/rotate/quota-history') return {ok:historyOK,json:async()=>quotaHistory};
  if(url==='/rotate/quota-rate') return {ok:quotaRateOK,json:async()=>quotaRate};
  return {ok:true,json:async()=>url==='/rotate/status'?status:stats};
}});
elements.tip=new Element();
elements.tip.parentNode=new Element();
(async()=>{
  vm.runInContext(script,context,{timeout:1000});
  await timers.get(10000)();
  assert(replaced.every(url=>!url.includes('key=')));
  assert(urls.every(({url,opts})=>!url.includes('key=')&&opts.headers.Authorization==='Bearer fake-viewer-key'&&opts.cache==='no-store'));
  assert.equal(urls.filter(({url})=>url==='/rotate/quota-history').length,1,'history fetched on open and separately from status');
  assert.equal(urls.filter(({url})=>url==='/rotate/quota-rate').length,1,'quota rates fetched on open and separately from status');
  assert.equal(elements['quota-rates']?.innerHTML||'','','a pending report has no rows');
  assert(timers.has(30000)&&!timers.has(300000),'a pending report retries in 30 seconds');
  quotaRate={computed_at:receipt-125,summary:[
    {account:value,limit:value,used_percent:82,usd_per_percent:{median:29.41},
     subscription_window_usd:48,value_multiple:{median:11.34,q1:10.24,q3:14.59},
     output_tokens_per_percent:25000,usd_left:1571.1,time_left:{hours:12.5},samples:21},
    {account:'OpenAI',limit:'images 24h',used_percent:0,usd_per_percent:null,output_tokens_per_percent:null,
     usd_left:value,time_left:{stale:true},samples:0},
    {account:'other',limit:'7d',subscription_window_usd:20,value_multiple:null}
  ],fits:[{account:value,relative_quota_per_dollar:2.33,fable_value_multiple:19.63,other_value_multiple:45.77},
         {account:'unpriced',relative_quota_per_dollar:2.1}],source_errors:{openai:[value]}};
  await timers.get(30000)();
  assert(timers.has(300000),'a completed report refreshes in five minutes');
  const rates=elements['quota-rates'].innerHTML, fits=elements['quota-rate-fits'].innerHTML;
  assert(!rates.includes('<mark')&&!fits.includes('<mark'));
  assert(rates.includes('&lt;mark')&&fits.includes('&lt;mark'),'quota-rate account, limit and fit labels are escaped');
  assert(rates.includes('11.3x')&&rates.includes('IQR 10.2–14.6x')&&!rates.includes('$29.41')
    && rates.includes('<td data-l="Value ($/$)">-</td>')&&rates.includes('$'+(1571).toLocaleString())
    && rates.includes('12.50 h')&&rates.includes('Projection stale'));
  assert(rates.includes('>0%</td>')&&rates.includes('Not enough data')&&elements['quota-rate-empty'].hidden);
  assert(fits.includes('Fable 2.3x the quota per $ of other models'));
  assert(fits.includes('Fable 19.6x, other 45.8x API $ per subscription $'));
  assert(fits.includes('Fable 2.1x the quota per $ of other models</div>'));
  assert.equal(elements['quota-rate-age'].textContent,'computed 2 min ago');
  assert.equal(elements['quota-rate-errors'].textContent,'OpenAI: '+value,'source errors are shown as text');
  assert.equal(elements['quota-rate-errors'].innerHTML,'');
  quotaRateOK=false;
  await timers.get(300000)();
  assert.equal(elements['quota-rates'].innerHTML,rates,'a failed fetch retains the report');
  assert.equal(elements['quota-rate-fits'].innerHTML,fits);
  quotaRateOK=true;
  quotaRate={computed_at:receipt,summary:[],fits:[]};
  await timers.get(300000)();
  assert.equal(elements['quota-rates'].innerHTML,'','empty report clears the table');
  assert(!elements['quota-rate-empty'].hidden&&!elements['quota-rate-fits'].innerHTML);
  assert.equal(elements['quota-rate-empty'].textContent,'Not enough data');
  assert(elements.weekly.innerHTML.includes('class="hit"'),'shortfall band not drawn');
  const {tip}=elements, scroll=()=>document.getElementById('wk-scroll');
  const touch=(x,y=30)=>({pointerType:'touch',pointerId:1,clientX:x,clientY:y});
  const tap=(chart,e)=>{chart.onpointerdown(e); chart.onpointerup(e); chart.onpointerleave(e)};
  const line=chart=>chart.querySelector('.crosshair');
  const mainPoint=touch(195);
  scroll().onpointerdown(mainPoint);
  assert.equal(tip.style.display,'none','touch down alone selects nothing');
  scroll().onpointerup(mainPoint);
  scroll().onpointerleave(mainPoint);
  assert.equal(tip.style.display,'block','the tooltip stays open after a chart tap');
  assert.equal(tip.style.pointerEvents,'auto','the touch tooltip can receive taps');
  assert.equal(line(scroll()).opacity,'.6');
  assert(allText(tip).includes('10%'));
  const oldScroll=scroll();
  status.weekly.series[1][1]=status.weekly.projection[0][1]=15;
  await timers.get(10000)();
  assert.notEqual(scroll(),oldScroll);
  assert.equal(line(scroll()).opacity,'.6','Claude restores the crosshair when the chart redraws');
  assert.equal(tip.style.display,'block');
  assert(allText(tip).includes('15%'),'restored Claude tooltip shows the new value');
  tip.onclick();
  assert.equal(tip.style.display,'none','tapping the tooltip hides it');
  assert.equal(line(scroll()).opacity,'0');
  tap(scroll(),mainPoint);
  scroll().onpointerdown(mainPoint);
  scroll().onpointercancel(mainPoint);
  scroll().onpointerup(mainPoint);
  assert.equal(tip.style.display,'none','a canceled tap clears the selection');
  scroll().onpointerdown(mainPoint);
  scroll().onpointermove(touch(215));
  scroll().onpointerup(touch(215));
  assert.equal(tip.style.display,'none','a touch drag does not select a point');
  tap(scroll(),mainPoint);
  scroll().scrollLeft+=10;
  scroll().onscroll();
  assert.equal(tip.style.display,'none','scrolling clears a touch selection');
  const mouse={...mainPoint,pointerType:'mouse'};
  scroll().onpointermove(mouse);
  assert.equal(tip.style.display,'block');
  assert.equal(tip.style.pointerEvents,'none','the mouse tooltip lets the pointer through');
  scroll().onpointerleave(mouse);
  assert.equal(tip.style.display,'none','leaving the chart with a mouse hides the tooltip');
  assert.equal(line(scroll()).opacity,'0');
  tap(scroll(),mainPoint);
  vm.runInContext('showTable=true',context);
  await timers.get(10000)();
  assert.equal(tip.style.display,'none','switching to the table hides the tooltip');
  vm.runInContext('showTable=false',context);
  await timers.get(10000)();
  tap(scroll(),mainPoint);
  status.weekly.series[1][0]=status.weekly.projection[0][0]=now+1;
  await timers.get(10000)();
  assert.equal(tip.style.display,'none','a vanished observation clears the tooltip');
  assert.equal(line(scroll()).opacity,'0');

  const [provider,empty]=elements.oai.children;
  const [summary,codex,images,inference]=provider.children[1].children;
  assert.equal(empty.children[1].children.length,1);
  assert.equal(empty.children[1].children[0].textContent,'No observations yet');
  assert(codex.innerHTML.includes('&lt;mark')&&!codex.innerHTML.includes('<mark'),'history limit label is escaped');
  assert(codex.innerHTML.includes('Last observed'));
  const plot=p=>p.querySelector('.quota-chart'), plotTip=p=>p.querySelector('.tip');
  const codexSVG=plot(codex).innerHTML;
  const paths=codexSVG.match(/class="quota-line" d="([^"]*)"/)[1];
  assert.equal((paths.match(/M/g)||[]).length,2,'a rollover, missing poll and drop split the path');
  assert.equal((paths.match(/L/g)||[]).length,3,'nearby polls with jitter join, but estimates drifting beyond the first do not');
  assert.equal((codexSVG.match(/class="quota-reset"/g)||[]).length,2,'jittered estimates share a reset marker');
  assert.equal((plot(images).innerHTML.match(/<circle/g)||[]).length,2,'sparse headers render as dots');
  assert.equal((plot(inference).innerHTML.match(/<circle/g)||[]).length,1,'a single observation yields a dot');
  assert(!plot(inference).innerHTML.includes('class="quota-reset"'),'implausible resets do not produce markers');
  const x=t=>40+(348-40)*(t-start)/(14*D);
  tap(plot(images),touch(x(now-D),126));
  assert.equal(plotTip(images).style.display,'block');
  assert.equal(line(plot(images)).opacity,'.6');
  assert(allText(plotTip(images)).includes('0%')&&allText(plotTip(images)).includes('100%'),'zero observation survives');
  assert.equal(line(plot(inference)).opacity,'0','crosshairs are local');
  assert.equal(line(scroll()).opacity,'0','OpenAI selection leaves Claude alone');
  await timers.get(10000)();
  assert.equal(provider.children[1].children[2],images,'status refresh keeps the OpenAI chart host');
  assert.equal(plotTip(images).style.display,'block','status refresh keeps OpenAI selection open');
  historyOK=false;
  await timers.get(60000)();
  assert.equal(provider.children[1].children[0],summary,'a failed history fetch keeps the chart and as-of timestamp');
  assert.equal(plotTip(images).style.display,'block');
  plotTip(images).onclick();
  assert.equal(plotTip(images).style.display,'none');
  assert.equal(line(plot(images)).opacity,'0');
  tap(plot(images),touch(x(now-5*D),126));
  assert.equal(plotTip(images).style.display,'none','a gap contains no selectable reading');
  tap(plot(inference),touch(x(now-2*D),112));
  assert.equal(plotTip(inference).style.display,'block','the single inference dot is selectable');
  assert(allText(plotTip(inference)).includes('Reported reset')&&!allText(plotTip(inference)).includes('Unknown'));
  historyOK=true;
  quotaHistory.as_of+=60;
  await timers.get(60000)();
  const fresh=provider.children[1].children[3];
  assert.notEqual(fresh,inference);
  assert.equal(plotTip(fresh).style.display,'block','history refresh restores a retained selection');
  assert.equal(line(plot(fresh)).opacity,'.6');

  assert(elements.devices.innerHTML.includes('removed'),'removed device not listed');
  for(const id of ['pills','accounts','oai','weekly','billing','alerts','devices','models','by-account','events']){
    assert(!html(elements[id]).includes('<mark'),id+' contains raw markup');
    assert(html(elements[id]).includes('&lt;mark'),id+' lost escaped text');
  }
  assert(!elements.billing.innerHTML.includes('ChatGPT plan'),'an unbilled provider has no subscription section');
  stats.provider_billing={[value]:{plan:value,reporting_plan:value,reported_plan:value,plan_mismatch:true,price:50,
    periods:[{start:'2026-11-01',end:'2026-12-01',plans:[value],price:50,days:30,elapsed:28,
              used:null,limited_h:null,usage:{cost_usd:150}},
             {start:'2026-10-17',end:'2026-11-01',plans:[value],price:25,days:15,elapsed:15,
              used:null,limited_h:null,usage:{cost_usd:150}}]}};
  status.providers[value].billing={plan:value,reported_plan:value};
  await timers.get(10000)();
  assert(!elements.billing.innerHTML.includes('<mark')&&elements.billing.innerHTML.includes('ChatGPT plan &lt;mark'));
  assert(elements.billing.innerHTML.includes('Plan differs')&&elements.billing.innerHTML.includes('3.0&times; the price'));
  assert(elements.billing.innerHTML.includes('6.0&times; the price'),'shortened period uses its prorated price for the ratio');
  assert(elements.billing.innerHTML.includes('$50/mo')&&elements.billing.innerHTML.includes('data-l="Price">$50</td>')
    && !elements.billing.innerHTML.includes('$50.00')&&elements.billing.innerHTML.includes('$200/mo'));
  const fractional=180.5.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
  assert(elements.billing.innerHTML.includes('data-l="Price">$'+fractional+'</td>'));
  assert(elements.billing.innerHTML.includes('<td data-l="Weekly use">–</td>')
     && elements.billing.innerHTML.includes('<td data-l="At its limit">&ndash;</td>'));
  assert(html(elements.oai).includes('Configured &lt;mark'));
  delete stats.provider_billing;
  delete status.providers[value].billing;
  await timers.get(10000)();
  const oai=html(elements.oai);
  assert.equal(oai.split('ChatGPT plan').length-1,2,'one quota card per openai-siwc provider, none for static-bearer');
  assert(!oai.includes(value+'-static')&&!oai.includes('&lt;mark data-audit=&quot;marker&quot;&gt;&amp;&#39;&quot;&lt;/mark&gt;-static'),'static-bearer provider got a quota card');
  assert(oai.includes('images ')&&oai.includes('title="&lt;mark'),'images badge or its escaped title missing');
  assert(oai.includes('>7d<')&&oai.includes('images · ')&&oai.includes('model · read only with an images login'),'quota rows or the empty card missing');
  assert(oai.includes('seen 2.0 h ago'),'a meter displays its window receipt time');
  assert(elements.pills.innerHTML.includes('>Rotate<')&&!elements.pills.innerHTML.includes('disabled'),'rotate button missing');
  assert(elements.accounts.innerHTML.includes('Resume auto')&&elements.accounts.innerHTML.includes('>Select<'),'account buttons missing');
  const claims=elements.accounts.innerHTML;
  const firstCard=claims.split('<div class="card">')[1];
  const footer=firstCard.indexOf('<div class="acct-badges">');
  assert(footer>firstCard.lastIndexOf('class="meter-row"')&&!firstCard.slice(0,footer).includes('class="badge"'));
  assert(firstCard.slice(footer).includes('Default active')&&firstCard.slice(footer).includes('renews'));
  assert.equal((claims.match(/class="meter-row"/g)||[]).length,3,'one meter for each current claim');
  assert(claims.includes('Fable 7d')&&claims.includes('>7d<')&&claims.includes('resets in')&&claims.includes('limited'));
  assert(claims.includes('seen 12 min ago')&&!claims.includes('seen 10 min ago'),'only notably older readings show their age');
  assert(!claims.includes('Default / Fable')&&!claims.includes('allowed')&&!claims.includes('awaiting reading'));
  assert.equal(claims.match(/rejected/g)?.length,1);
  assert(!claims.includes('limit reached')&&!claims.includes('blocked'));
  assert(!claims.includes('>5h<')&&!claims.includes('<b>0%</b>'),'absent and expired usage do not show zero');
  Object.assign(status.accounts[value].claims['7d_oi'],{effective_utilization:null,freshness:'awaiting_reading',
    reset:receipt-60,blocked_until:receipt+300});
  await timers.get(10000)();
  const recheck=elements.accounts.innerHTML;
  assert(recheck.includes('Fable 7d')&&recheck.includes('rejected')&&recheck.includes('recheck')
    &&!recheck.includes('<b>100%</b>'),'an active block still renders without stale utilization');
  const pills=elements.pills.innerHTML;
  assert(pills.includes('Default: ')&&pills.includes('Fable: ')&&pills.includes('&lt;/mark&gt;2</span>'));
  assert(elements.devices.innerHTML.includes('strict (config)')&&elements.devices.innerHTML.includes('data-pin='));
  status.active_by_family={default:value+'2',fable:value+'2'};
  status.manual_by_family={default:null,fable:null};
  status.accounts[value].eligible_by_family.default=false;
  await timers.get(10000)();
  assert.equal((elements.accounts.innerHTML.match(/>Select</g)||[]).length,1,
    'the only eligible account has Select even though it is active for both families');
  assert(elements.pills.innerHTML.includes('disabled title="no other account is eligible"'),
    'Rotate cannot select the same account');
  elements.accounts.onclick({target:{closest:()=>({dataset:{sw:value+'2'}})}});
  assert(elements.confirm.open,'confirmation not shown');
  assert(!elements['cf-text'].innerHTML.includes('<mark')&&elements['cf-text'].innerHTML.includes('&lt;mark'),'confirmation not escaped');
  await elements['cf-yes'].onclick();
  const post=urls.find(u=>u.url==='/rotate/switch');
  assert(post&&post.opts.method==='POST'&&JSON.parse(post.opts.body).account===value+'2'
    &&post.opts.headers.Authorization==='Bearer fake-viewer-key'&&post.opts.headers['Content-Type']==='application/json','switch request');
  assert(!elements.confirm.open,'confirmation stays open after a switch');
  status.manual_by_family={default:value+'2',fable:value+'2'};
  await timers.get(10000)();
  assert(elements.accounts.innerHTML.includes('Resume auto')&&!elements.accounts.innerHTML.includes('>Select<'));
  const edit=()=>elements.devices.onclick({target:{closest:()=>({dataset:{pin:value}})}});
  edit();
  assert(elements['pin-form'].open&&elements['pin-target'].hidden&&elements['pin-mode'].value==='use_config');
  assert(elements['pin-title'].textContent.includes(value)&&!elements['pin-account'].innerHTML.includes('<mark'));
  elements['pin-mode'].value='fallback';
  elements['pin-mode'].onchange();
  elements['pin-account'].value=value+'2';
  assert(!elements['pin-target'].hidden);
  await timers.get(10000)();
  assert(elements['pin-form'].open&&elements['pin-account'].value===value+'2','refresh preserves the open pin editor');
  await elements['pin-save'].onclick();
  edit();
  elements['pin-mode'].value='auto';
  await elements['pin-save'].onclick();
  status.device_pins[value]={account_pin:null,source:'runtime'};
  edit();
  assert(elements['pin-mode'].value==='auto');
  elements['pin-mode'].value='use_config';
  await elements['pin-save'].onclick();
  const pinPosts=urls.filter(u=>u.url==='/rotate/device-pin');
  assert.equal(pinPosts.length,3);
  assert(pinPosts.every(({opts})=>opts.method==='POST'&&opts.headers.Authorization==='Bearer fake-viewer-key'
    &&opts.headers['Content-Type']==='application/json'&&opts.cache==='no-store'));
  assert.deepEqual(pinPosts.map(({opts})=>JSON.parse(opts.body)),[
    {device:value,account_pin:{mode:'fallback',account:value+'2'}},
    {device:value,account_pin:null},
    {device:value,use_config:true}
  ]);
  console.log('panel JavaScript security checks passed');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    subprocess.run([node, "-e", harness, json.dumps(script)], check=True)


if __name__ == "__main__":
    main()

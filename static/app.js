'use strict';
const TAGS=[['pH_Reading','EQ pH','pH'],['EQ_Tank_Level_Ft','EQ Tank Level','ft'],['FLOW_RATE','Discharge Flow','L/min'],['Total_Flow_Li','Total Flow','L']];
const ICONS={
 pH_Reading:'<svg viewBox="0 0 32 32"><path d="M12 4h8M13 4v9L6.5 25a2 2 0 0 0 1.8 3h15.4a2 2 0 0 0 1.8-3L19 13V4"/><path d="M9.5 20h13"/></svg>',
 EQ_Tank_Level_Ft:'<svg viewBox="0 0 32 32"><rect x="4" y="7" width="24" height="21" rx="2.5"/><path d="M10 4v5M22 4v5M4 13h24"/><path d="M8 21c2-2 4-2 6 0s4 2 6 0 4-2 4-2"/></svg>',
 FLOW_RATE:'<svg viewBox="0 0 32 32"><path d="M5.5 24a12 12 0 1 1 21 0"/><path d="M16 18l6-6"/><circle cx="16" cy="18" r="1.6"/><path d="M9 11l1.5 1.5M16 8v2M23 11l-1.5 1.5M7 18h2M25 18h-2"/></svg>',
 Total_Flow_Li:'<svg viewBox="0 0 32 32"><ellipse cx="16" cy="7.5" rx="10" ry="3.5"/><path d="M6 7.5v17c0 1.9 4.5 3.5 10 3.5s10-1.6 10-3.5v-17"/><path d="M6 13.2c0 1.9 4.5 3.5 10 3.5s10-1.6 10-3.5M6 18.9c0 1.9 4.5 3.5 10 3.5s10-1.6 10-3.5"/></svg>'
};
const $=id=>document.getElementById(id);
const fmt=x=>x==null?'--.--':Number(x).toLocaleString('en-CA',{minimumFractionDigits:2,maximumFractionDigits:2});
const local=x=>{try{return new Intl.DateTimeFormat('en-CA',{timeZone:'America/Vancouver',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',hour12:true}).format(new Date(x))}catch{return '—'}};
let summaryData=null;let historyData=null;let userRole='Operator';let userName='';let clockOffset=0;let perms=new Set();let config={};
const VIEW_PERMS={settings:'settings',audit:'audit',reports:'export'};
const can=p=>perms.has(p);
// Discharge flow display unit, remembered per browser. The server sends L/min (raw US gal/min x 3.78541).
const GAL_PER_L=1/3.78541;
let doseUnit='ml/min';try{if(localStorage.getItem('ww.doseUnit')==='ml/h')doseUnit='ml/h'}catch{}
let flowUnit='L/min';try{if(localStorage.getItem('ww.flowUnit')==='gal/min')flowUnit='gal/min'}catch{}
const flowValue=v=>v==null?v:(flowUnit==='gal/min'?v*GAL_PER_L:v);
const unitFor=(key,unit)=>key==='FLOW_RATE'?flowUnit:unit;
function setFlowUnit(u){flowUnit=u==='gal/min'?'gal/min':'L/min';try{localStorage.setItem('ww.flowUnit',flowUnit)}catch{};drawCards();if(historyData&&!$('trends').hidden)chartRows('historyCharts')}
function applyRole(){document.querySelectorAll('.accounts').forEach(p=>p.hidden=!can('accounts'));document.querySelectorAll('nav button').forEach(b=>{const need=VIEW_PERMS[b.dataset.view];b.hidden=!!need&&!can(need)});const open=document.querySelector('.view:not([hidden])');if(open&&VIEW_PERMS[open.id]&&!can(VIEW_PERMS[open.id]))view('overview')}
async function api(path,opts={}){const response=await fetch(path,{cache:'no-store',...opts});if(!response.ok)throw Error('HTTP '+response.status);return response.json()}
function ageText(stamp){if(!stamp)return 'No readings';const s=Math.max(0,Math.round((Date.now()+clockOffset-Date.parse(stamp))/1000));return 'Updated '+(s<60?s+' s':s<3600?Math.floor(s/60)+' min':Math.floor(s/3600)+' h '+Math.floor(s%3600/60)+' min')+' ago'}
function tickAges(){document.querySelectorAll('[data-stamp]').forEach(el=>el.textContent=ageText(el.dataset.stamp));document.querySelectorAll('[data-since]').forEach(el=>el.textContent=sinceText(el.dataset.since))}
// How long an alarm has been (or was) active: "12 min", "1 h 5 min"
function durText(ms){const m=Math.max(0,Math.floor(ms/60000));if(m<60)return m+' min';if(m>=1440){const h=Math.floor(m%1440/60);return Math.floor(m/1440)+' d'+(h?' '+h+' h':'')}return Math.floor(m/60)+' h'+(m%60?' '+m%60+' min':'')}
function sinceText(stamp){return 'active '+durText(Date.now()+clockOffset-Date.parse(stamp))}
function durCell(r){if(isActive(r))return `<span class="since" data-since="${escapeHTML(r.stamp)}">${sinceText(r.stamp)}</span>`;return r.cleared_at?durText(Date.parse(r.cleared_at)-Date.parse(r.stamp)):'—'}
function escapeHTML(x){return String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function view(name){if(VIEW_PERMS[name]&&!can(VIEW_PERMS[name]))name='overview';document.body.dataset.view=name;document.querySelectorAll('.view').forEach(n=>n.hidden=n.id!==name);document.querySelectorAll('nav button').forEach(n=>n.classList.toggle('selected',n.dataset.view===name));if(name==='events'){loadEvents();loadShelves();loadInsights()}if(name==='reports'){loadPickups();loadPurchases();loadStock();loadCost()}if(name==='audit'){loadAudit();loadCalibrations()}if(name==='settings')loadSettings();if(name==='trends')loadHistory()}
document.querySelectorAll('nav button').forEach(b=>b.addEventListener('click',()=>view(b.dataset.view)));
// Predictive pH: where pH is heading (server: least-squares trend of the last 30 minutes by default).
function trendBadge(){const tr=summaryData.ph_trend;if(!tr||tr.rate_per_h==null)return '';const r=tr.rate_per_h;
 const arrow=Math.abs(r)<0.1?'→':r>0?'↗':'↘';const warn=!!tr.direction;
 const tip=warn?`pH heading for the ${Number(tr.limit).toFixed(2)} limit: about ${Math.max(0,Math.round(tr.eta_min))} minutes at ${Math.abs(r).toFixed(2)} pH per hour`:`pH trend: ${r>=0?'+':''}${r.toFixed(2)} pH per hour`;
 return `<span class="ph-trend${warn?' warn':''}" title="${escapeHTML(tip)}">${arrow} ${r>=0?'+':''}${r.toFixed(2)}/h</span>`}
function drawCards(){
 if(!summaryData)return;
 const lookup=Object.fromEntries(summaryData.tags.map(t=>[t.name,t]));
 $('cards').innerHTML=TAGS.map(([key,label,unit])=>{
   const item=lookup[key]||{};const status=item.status||'missing';const good=status==='current'&&item.value!=null;
   const pill=good?'LIVE':status.toUpperCase();
   const tip=`${label}: ${status} · ${item.stamp?local(item.stamp):'No records'}`;
   return `<div class="card ${good?'':'not-live'}" title="${escapeHTML(tip)}"><div class="card-icon" aria-hidden="true">${ICONS[key]||''}</div><div class="card-meta"><div class="name">${escapeHTML(label)}</div><span class="pill-row"><span class="live-pill ${escapeHTML(status)}"><i class="state-dot ${escapeHTML(status)}"></i>${escapeHTML(pill)}</span></span><small class="age" data-stamp="${escapeHTML(item.stamp||'')}">${escapeHTML(ageText(item.stamp))}</small></div><div class="card-divider" aria-hidden="true"></div><div class="value">${good?fmt(key==='FLOW_RATE'?flowValue(item.value):item.value):'--.--'}${key==='FLOW_RATE'?`<select class="unit unit-select" data-flow-unit aria-label="Discharge flow unit" title="Click to show litres or US gallons per minute"><option value="L/min"${flowUnit==='L/min'?' selected':''}>L/min</option><option value="gal/min"${flowUnit==='gal/min'?' selected':''}>gal/min</option></select>`:`<span class="unit">${unit}</span>`}${key==='pH_Reading'&&good?trendBadge():''}</div></div>`;
 }).join('');
 document.querySelectorAll('[data-flow-unit]').forEach(s=>s.onchange=()=>setFlowUnit(s.value));
 const item=(key)=>lookup[key]||{};
 const set=(id,key,unit='')=>{const t=item(key);$(id).textContent=(t.status==='current'&&t.value!=null?fmt(t.value):'--.--')+(unit?' '+unit:'')};
 set('tankPh','pH_Reading');set('tankLevel','EQ_Tank_Level_Ft');{const t=item('FLOW_RATE');$('diagramFlow').textContent=(t.status==='current'&&t.value!=null?fmt(flowValue(t.value)):'--.--')+' '+flowUnit}
 {const ex=summaryData.extras||{};const show=(id,tag,unit,dec)=>{const r=ex[tag]||{};const ok=r.status==='current'&&r.value!=null;const el=$(id);el.textContent=(ok?Number(r.value).toFixed(dec):'--.'+'-'.repeat(dec))+' '+unit;el.classList.toggle('live',ok);el.title=`${r.label||tag} (${tag}): ${r.status||'missing'}${r.stamp?' · '+local(r.stamp):''}`};
  // Dosing run rates arrive in ml/min (the server converts PLC ml/h); shown in the unit picked here.
  const dose=(id,tag)=>{const r=ex[tag]||{};const ok=r.status==='current'&&r.value!=null;const el=$(id);el.textContent=ok?(doseUnit==='ml/h'?(r.value*60).toFixed(1):Number(r.value).toFixed(2)):'--.--';el.classList.toggle('live',ok);
   el.title=`${r.label||tag} (${tag}): ${r.status||'missing'}${r.raw_value!=null?' · PLC value '+Number(r.raw_value).toFixed(2)+' '+r.raw_unit:''}${r.stamp?' · '+local(r.stamp):''}`};
  document.querySelectorAll('[data-dose-unit]').forEach(s=>{if(s.value!==doseUnit)s.value=doseUnit;s.onchange=()=>{doseUnit=s.value==='ml/h'?'ml/h':'ml/min';try{localStorage.setItem('ww.doseUnit',doseUnit)}catch{};drawCards()}});
  show('rakeSpeed','Rake_Speed_VFD','Hz',1);show('coagRate','Coag_Pump_Rate_Percent','%',1);dose('coagRun','Coag_Pump_Run');show('flocRate','Floc_Pump_Rate_Percent','%',1);dose('flocRun','Floc_Pump_Run');
  // Dosing pump run status: RUNNING while its run reading is at least 0.01 ml/min (the server's DOSE_RUNNING_MIN).
  // A smaller leftover reading shows as 0.00 ml/min, so it counts as STOPPED too.
  for(const [id,tag] of [['coagState','Coag_Pump_Run'],['flocState','Floc_Pump_Run']]){const r=ex[tag]||{};const known=r.status==='current'&&r.value!=null;const v=Number(r.value);const on=known&&v>=0.01;
   const el=$(id);el.textContent=known?(on?'● RUNNING':'STOPPED'):'--';el.className='dose-state'+(known?(on?' on':' off'):'');
   el.title=known?`${on?'RUNNING':'STOPPED'}: run reading ${v.toFixed(4)} ml/min${r.raw_value!=null?` (PLC value ${Number(r.raw_value).toFixed(2)} ${r.raw_unit})`:''}; below 0.01 ml/min counts as stopped`:`${tag}: ${r.status||'missing'}`}
  {const r=ex.SLUDGE_TANK_LEVEL||{};const known=r.status==='current'&&r.value!=null;const full=known&&Number(r.value)>=1;
   $('sludgeState').textContent=known?(full?'FULL':'NORMAL'):'--';$('sludgeTank').className='sludge-tank'+(full?' full':known?' normal':'');
   $('sludgeTank').title=`Sludge tank level switch (SLUDGE_TANK_LEVEL, PLC B3:0/13): ${known?(full?'FULL':'not full'):(r.status||'missing')}${r.stamp?' · since '+local(r.stamp):''}`;
   const pump=(id,tag)=>{const r=ex[tag]||{};const known=r.status==='current'&&r.value!=null;const on=known&&Number(r.value)!==0;
    for(const el of [$(id),$(id+'Pill')]){el.classList.toggle('on',on);el.classList.toggle('off',known&&!on);el.title=`${r.label||tag} (${tag}): ${known?(on?'RUNNING':'OFF'):(r.status||'missing')}${r.stamp?' · since '+local(r.stamp):''}`}
    $(id+'Pill').textContent=known?(on?'● RUNNING':'OFF'):'--'};
   pump('sumpPump','SUMP_PUMP_STATUS');pump('mixerPump','Mixer_Pump_Staus');
   {const last=summaryData.last_pickup;$('lastPickup').textContent=last?local(last):'none logged'}}
  // System ON / OFF circle (SYSTEM_ENABLED, PLC B3:0/4: 1 = on)
  {const r=ex.SYSTEM_ENABLED||{};const known=r.status==='current'&&r.value!=null;const on=known&&Number(r.value)>=0.5;
   $('systemState').className='system-state'+(known?(on?' on':' off'):'');$('systemText').textContent=known?(on?'ON':'OFF'):'--';
   $('systemState').title=`System enabled (SYSTEM_ENABLED, PLC B3:0/4): ${known?(on?'ON':'OFF'):(r.status||'missing')}${r.stamp?' · since '+local(r.stamp):''}`}
  // Alarm banner: one line per PLC alarm bit that is 1 right now (sludge tank FULL, E-Stop, faults)
  {const shelf=new Set((summaryData.shelved||[]).map(s=>s.key));
   const lines=(summaryData.plc_alarms||[]).filter(a=>{const r=ex[a.tag]||{};return !shelf.has('plc:'+a.tag)&&r.status==='current'&&r.value!=null&&Number(r.value)>=0.5})
    .map(a=>{const r=ex[a.tag];const [head,...rest]=a.banner.split(' · ');return `<div>⚠ ${escapeHTML(head)}${r.stamp?' since '+escapeHTML(local(r.stamp)):''}${rest.length?' · '+escapeHTML(rest.join(' · ')):''}</div>`});
   const rib=$('alarmRibbon');rib.hidden=!lines.length;rib.innerHTML=lines.join('');
   const sh=summaryData.shelved||[];$('shelfNote').hidden=!sh.length;
   $('shelfNote').textContent=sh.length?'Shelved (no emails or texts): '+sh.map(s=>`${s.label} until ${local(s.until)} — ${s.reason}`).join(' · '):''}}
 const ph=item('pH_Reading');const limits=summaryData.limits||{low:5.8,high:9};const banner=$('banner');banner.className='banner';
 const allCurrent=summaryData.tags.every(x=>x.status==='current');
 const outside=ph.status==='current'&&ph.value!=null&&(ph.value<limits.low||ph.value>limits.high);
 const age=summaryData.data_age_seconds;const staleMin=limits.stale_minutes||15;
 if(age!=null&&age>staleMin*60){banner.classList.add('alert');banner.textContent=`NO NEW DATA for ${Math.floor(age/60)} minutes · check the Ewon, Talk2M and the collector`;}
 else if(outside){banner.classList.add('alert');banner.textContent=`pH ${fmt(ph.value)} outside ${fmt(limits.low)}–${fmt(limits.high)} · ${limits.enabled?'Alarm monitoring enabled':'Alarm monitoring disabled'}`;}
 else if(summaryData.ph_trend&&summaryData.ph_trend.direction){const tr=summaryData.ph_trend;banner.classList.add('warn');
  banner.textContent=`pH ${fmt(tr.value)} ${tr.direction==='high'?'rising':'falling'} ${Math.abs(tr.rate_per_h).toFixed(2)} per hour · may reach the ${fmt(tr.limit)} limit in about ${Math.max(0,Math.round(tr.eta_min))} minutes · check the ${tr.direction==='high'?'acid':'caustic'} dosing`}
 else if(allCurrent){banner.classList.add('ok');banner.innerHTML=`<span class="live-word">● LIVE READING</span> · <span id="liveClock">${escapeHTML(liveNow())}</span> · WASTEWATER`}
 else{banner.textContent='Telemetry warning: one or more readings are missing, invalid or stale — check the collector.'}
 const active=summaryData.active_alarms||[];const box=$('activeAlarms');box.hidden=!active.length;
 const live=active.filter(isActive).length;box.classList.toggle('unacked',live>0);
 $('activeSummary').textContent=`Active alarms: ${live}`+(active.length>live?` · ${active.length-live} cleared`:'')+' — click to '+(box.open?'hide':'show');
 $('activeList').innerHTML=active.map(r=>`<li><b>${escapeHTML(r.kind)}</b>${isActive(r)?` <span class="since" data-since="${escapeHTML(r.stamp)}">${sinceText(r.stamp)}</span>`:''} — ${escapeHTML(r.detail)} <small>(${escapeHTML(local(r.stamp))}, ${escapeHTML(stateText(r))})</small></li>`).join('');
}
// Current Vancouver date and time for the green LIVE READING bar (server clock, ticks every second)
const LIVE_FMT=new Intl.DateTimeFormat('en-CA',{timeZone:'America/Vancouver',weekday:'short',year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',second:'2-digit',hour12:true});
const liveNow=()=>LIVE_FMT.format(new Date(Date.now()+clockOffset));
setInterval(()=>{const el=$('liveClock');if(el)el.textContent=liveNow()},1000);
const VAN_TIME=new Intl.DateTimeFormat('en-CA',{timeZone:'America/Vancouver',hour:'numeric',minute:'2-digit',hour12:true});
const VAN_DAY=new Intl.DateTimeFormat('en-CA',{timeZone:'America/Vancouver',month:'short',day:'numeric'});
const VAN_HOUR=new Intl.DateTimeFormat('en-US',{timeZone:'America/Vancouver',hour:'numeric',minute:'numeric',hourCycle:'h23'});
function vanHM(ts){const [h,m]=VAN_HOUR.format(new Date(ts)).split(':').map(Number);return [h%24,m]}
// Time ticks for the x axis: every 30 minutes for windows up to a day, every 6 hours (Vancouver time) for longer ones.
function timeTicks(start,end){const ticks=[];const long=end-start>26*3600000;const step=long?3600000:1800000;
 for(let t=Math.ceil(start/step)*step;t<=end;t+=step){const [h,m]=vanHM(t);if(long&&(m!==0||h%6))continue;ticks.push({t,midnight:h===0&&m===0})}return {ticks,long}}
function plot(canvas,points,tag){const W=Math.max(280,canvas.clientWidth||600),H=172,dpr=window.devicePixelRatio||1;canvas.width=W*dpr;canvas.height=H*dpr;const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);ctx.clearRect(0,0,W,H);
 ctx.fillStyle='#000';ctx.fillRect(0,0,W,H);
 const left=58,right=W-14,top=12,bottom=128;ctx.font='11px system-ui';ctx.lineWidth=1;
 const start=Date.parse(historyData.from),end=Date.parse(historyData.to);const X=ts=>left+((ts-start)/(end-start))*(right-left);
 // vertical time grid and labels (labels thinned so they never overlap)
 const {ticks,long}=timeTicks(start,end);const px=ticks.length>1?X(ticks[1].t)-X(ticks[0].t):999;const every=Math.max(1,Math.ceil((long?78:62)/px));
 ticks.forEach((k,n)=>{const x=X(k.t);ctx.strokeStyle=k.midnight?'#3d6a86':'#1d3443';ctx.beginPath();ctx.moveTo(x,top);ctx.lineTo(x,bottom);ctx.stroke();
  if(n%every)return;ctx.fillStyle='#b4c9d8';ctx.textAlign='center';const d=new Date(k.t);
  const put=(txt,y)=>{const w=ctx.measureText(txt).width/2;ctx.fillText(txt,Math.min(Math.max(x,left+w),right-w),y)};
  put(long?VAN_DAY.format(d)+(k.midnight?'':' '+VAN_TIME.format(d)):VAN_TIME.format(d),bottom+16);
  if(!long&&k.midnight){ctx.fillStyle='#7fa3b8';put(VAN_DAY.format(d),bottom+30)}});
 ctx.textAlign='left';
 const valid=points.filter(p=>Number.isFinite(p.value));ctx.strokeStyle='#33536a';ctx.fillStyle='#a9c1d2';
 if(!valid.length){ctx.fillText('No samples for this period',left+10,65);return}
 let mn=Math.min(...valid.map(p=>p.value)),mx=Math.max(...valid.map(p=>p.value));const pad=Math.max((mx-mn)*.14,Math.abs(mx)*.0004,.01);mn-=pad;mx+=pad;
 for(let i=0;i<=3;i++){let y=top+i*(bottom-top)/3;ctx.strokeStyle='#2a4557';ctx.beginPath();ctx.moveTo(left,y);ctx.lineTo(right,y);ctx.stroke();ctx.fillStyle='#a9c1d2';ctx.fillText(fmt(mx-(mx-mn)*i/3),0,y+4)}
 ctx.beginPath();let old=null;valid.forEach((p)=>{let ts=Date.parse(p.date);if(!Number.isFinite(ts))return;let x=X(ts),y=bottom-(p.value-mn)/(mx-mn)*(bottom-top);if(old&&ts-old.ts<=180000)ctx.lineTo(x,y);else ctx.moveTo(x,y);old={ts}});ctx.strokeStyle='#60d9ce';ctx.lineWidth=2;ctx.stroke()}
function chartRows(id){if(!summaryData||!historyData)return;const values=Object.fromEntries(summaryData.tags.map(t=>[t.name,t]));const root=$(id);root.innerHTML='';TAGS.forEach(([name,label,unit])=>{const div=document.createElement('div');div.className='trendrow';div.innerHTML=`<div class="trendmetric"><div class="trend-icon" aria-hidden="true">${ICONS[name]||''}</div><div><span>${label}</span><b>${fmt(name==='FLOW_RATE'?flowValue(values[name]?.value):values[name]?.value)} <small>${unitFor(name,unit)}</small></b><small>${escapeHTML(values[name]?.status||'missing')}</small></div></div><div><div class="charttitle">Historical trend · ${label} · ${escapeHTML(local(historyData.from))} – ${escapeHTML(local(historyData.to))}</div><canvas aria-label="${label} trend"></canvas></div>`;root.append(div);plot(div.querySelector('canvas'),(historyData.series[name]||[]).map(p=>name==='FLOW_RATE'?{...p,value:flowValue(p.value)}:p),name)})}
async function loadInsights(){try{const d=await api('/api/insights');
 $('insightList').innerHTML=d.insights.map(i=>`<li class="insight ${escapeHTML(i.severity)}"><b>${escapeHTML(i.title)}</b><span>${escapeHTML(i.detail)}</span><span class="insight-action">Suggested action: ${escapeHTML(i.action)}</span></li>`).join('')}
 catch(e){$('insightList').innerHTML=`<li>Insights unavailable: ${escapeHTML(e.message)}</li>`}}
async function loadSummary(){try{summaryData=await api('/api/summary');userRole=summaryData.role;userName=summaryData.user||'';clockOffset=Date.parse(summaryData.now)-Date.now()||0;perms=new Set(summaryData.permissions||[]);$('role').textContent=userName?`${userRole} · ${userName}`:userRole;applyRole();drawCards()}catch(e){$('banner').textContent='Unable to load API-backed stored readings: '+e.message}}
async function loadHistory(overview=false){const hours=Number($('period').value);try{historyData=await api('/api/history?from='+encodeURIComponent(new Date(Date.now()-hours*3600000).toISOString())+'&to='+encodeURIComponent(new Date().toISOString()));chartRows('historyCharts')}catch(e){$('historyCharts').textContent='History unavailable: '+e.message}}
// 'open' + cleared_at: the PLC alarm has cleared but nobody acknowledged it yet.
// Alarms are acknowledged automatically when recorded: an alarm is 'active' until it clears, then 'closed'.
const isActive=r=>(r.state==='open'||r.state==='acknowledged')&&!r.cleared_at;
function stateText(r){const auto=!r.actor||String(r.actor).startsWith('system (auto');if(isActive(r))return auto?'active · auto-acknowledged':'active · acknowledged by '+r.actor;return auto||r.state!=='closed'?'closed':'closed · acknowledged by '+r.actor}
async function loadEvents(){try{const data=await api('/api/events');$('eventlist').innerHTML=`<div class="tablewrap"><table><thead><tr><th>Time</th><th>Event</th><th>Detail</th><th>Duration</th><th>Status</th></tr></thead><tbody>${data.rows.map(r=>`<tr><td>${local(r.stamp)}</td><td>${escapeHTML(r.kind)}</td><td>${escapeHTML(r.detail)}</td><td>${durCell(r)}</td><td>${escapeHTML(stateText(r))}</td></tr>`).join('')||'<tr><td colspan="5">No recorded alarms yet.</td></tr>'}</tbody></table></div>`;document.querySelectorAll('[data-ack]').forEach(b=>b.onclick=async()=>{try{await api('/api/ack',{method:'POST',headers:{'Content-Type':'application/json','X-WW-Request':'1'},body:JSON.stringify({id:Number(b.dataset.ack)})});loadEvents()}catch(e){alert('Could not acknowledge: '+e.message)}})}catch(e){$('eventlist').textContent=e.message}}
async function loadAudit(){try{const data=await api('/api/audit');$('auditlist').innerHTML=`<div class="tablewrap"><table><thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Detail</th></tr></thead><tbody>${data.rows.map(r=>`<tr><td>${local(r.stamp)}</td><td>${escapeHTML(r.actor)}</td><td>${escapeHTML(r.action)}</td><td>${escapeHTML(r.detail)}</td></tr>`).join('')||'<tr><td colspan="4">No audit entries.</td></tr>'}</tbody></table></div>`}catch(e){$('auditlist').textContent='Admin required or audit unavailable: '+e.message}}
const KEYS=['ph_low','ph_high','alarms_enabled','email_enabled','email_recovery_enabled','alarm_confirm_samples','alarm_recovery_samples','ph_repeat_min','alarm_on','alarm_off','daily_enabled','daily_time','monthly_enabled','monthly_day','monthly_time',
 'stale_alert_enabled','stale_alert_minutes','level_alarm_enabled','level_high_ft','flow_alarm_enabled','flow_low_lpm','flow_low_minutes','quarterly_enabled','plc_alarms_enabled','cal_reminders_enabled','cal_ph_days','cal_level_days','cal_remind_days',
 'stock_alarm_enabled','sms_enabled',
 'ph_trend_enabled','ph_trend_window_min','ph_trend_lookahead_min','dosing_alarm_enabled','dosing_alarm_minutes'];
const dayNames=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];$('alarmDays').innerHTML=dayNames.map((d,i)=>`<label><input type="checkbox" data-day="${i}"> ${d}</label>`).join('');
async function loadSettings(){if(can('accounts'))loadAccounts();try{let r=await api('/api/settings');config=r.settings;KEYS.forEach(k=>{if($(k))$(k)[$(k).type==='checkbox'?'checked':'value']=config[k]});document.querySelectorAll('[data-day]').forEach(x=>x.checked=config.alarm_days.includes(Number(x.dataset.day)));$('recipients').value=(config.recipients||[]).join('\n');$('sms_recipients').value=(config.sms_recipients||[]).join('\n');renderProducts(config.chemical_products||[]);$('smtp').textContent='SMTP credentials: '+(r.email_configured?'file present (delivery not verified)':'not configured — email will not send');document.querySelectorAll('#settings input:not([data-fixed]),#settings textarea,#settings button,#productRows select').forEach(el=>el.disabled=!can('settings'))}catch(e){$('saveState').textContent=e.message}}
// ---- Accounts and passwords (Admin only; server enforces the same rule) ----
const postJSON=(path,body)=>fetch(path,{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json','X-WW-Request':'1'},body:JSON.stringify(body)}).then(async r=>{const d=await r.json().catch(()=>({}));if(!r.ok)throw Error(d.error||('HTTP '+r.status));return d});
function accountMsg(text,ok){const el=$('accountState');el.textContent=text;el.className=ok?'ok-text':'error-text'}
async function loadAccounts(){try{const a=await api('/api/accounts');
 $('adminState').textContent=`Signed-in Admin: ${a.admin.username||'—'}`+(a.admin.source==='file'?' (original server login)':'');
 $('adminUser').value=a.admin.username||'';
 const users=a.users||[];
 $('userState').textContent=users.length?`${users.length} login${users.length>1?'s':''}:`:'None set up — only the Admin can sign in';
 $('userList').innerHTML=users.map(u=>`<li><span><b class="role-tag">${escapeHTML(u.role)}</b> ${escapeHTML(u.username)}${u.source==='file'?' <small>(original server login)</small>':''}</span><button type="button" class="danger" data-remove-user="${escapeHTML(u.username)}" data-remove-role="${escapeHTML(u.role)}">Remove</button></li>`).join('');
 document.querySelectorAll('[data-remove-user]').forEach(b=>b.onclick=async()=>{const name=b.dataset.removeUser,role=b.dataset.removeRole;if(!confirm(`Remove the ${role} login "${name}"? They will no longer be able to sign in.`))return;
  try{await postJSON('/api/accounts/user/remove',{role,username:name});accountMsg(`${role} login ${name} removed.`,true);loadAccounts()}catch(e){accountMsg('Not removed: '+e.message)}});
 $('opUser').value='';$('disableOperator').disabled=!users.some(u=>u.role==='Operator');
 const P=a.permissions||{},choices=a.permission_choices||[];
 $('rightsBody').innerHTML=[['view','View readings, trends and alarms']].concat(choices.map(c=>[c,RIGHT_LABELS[c]||c])).map(([key,label])=>
  `<tr><th>${escapeHTML(label)}</th>`+RIGHT_ROLES.map(r=>{const fixed=r==='Admin'||key==='view';const on=fixed||(P[r]||[]).includes(key);
   return `<td><input type="checkbox" aria-label="${escapeHTML(r+': '+label)}" data-right="${key}" data-right-role="${r}"${on?' checked':''}${fixed?' disabled data-fixed':''}></td>`}).join('')+'</tr>').join('');
}catch(e){$('adminState').textContent=$('userState').textContent='Unavailable: '+e.message}}
$('saveAdmin').addEventListener('click',async()=>{
 if($('adminNew').value!==$('adminRepeat').value){accountMsg('New Admin passwords do not match.');return}
 if(!confirm('Change the Admin login? You will be asked to sign in again with the new password.'))return;
 try{await postJSON('/api/accounts/admin',{username:$('adminUser').value.trim(),current_password:$('adminCurrent').value,new_password:$('adminNew').value});
  ['adminCurrent','adminNew','adminRepeat'].forEach(id=>$(id).value='');
  accountMsg('Admin login changed. Sign in again with the new password…',true);
  setTimeout(()=>window.location.reload(),1500);
 }catch(e){accountMsg('Not changed: '+e.message)}});
const RIGHT_ROLES=['Admin','Engineering','Operator','Guest'];
const RIGHT_LABELS={pickup:'Log sludge pickups and chemical stock',export:'Download reports (Excel and CSV)',acknowledge:'Acknowledge alarms',settings:'View and change Settings (alarms, email reports) and shelve alarms',audit:'View the Audit log'};
const MIN_PW={Engineering:6,Operator:4,Guest:4};
$('newRole').addEventListener('change',()=>{$('pwMin').textContent=MIN_PW[$('newRole').value]});
$('saveOperator').addEventListener('click',async()=>{
 const role=$('newRole').value;
 if($('opPass').value!==$('opRepeat').value){accountMsg('Passwords do not match.');return}
 try{const r=await postJSON('/api/accounts/user',{role,username:$('opUser').value.trim(),password:$('opPass').value});
  ['opPass','opRepeat'].forEach(id=>$(id).value='');$('opUser').value='';accountMsg(`${role} login saved for ${r.username}.`,true);loadAccounts();
 }catch(e){accountMsg('Not saved: '+e.message)}});
$('saveRights').addEventListener('click',async()=>{
 const body={Engineering:[],Operator:[],Guest:[]};
 document.querySelectorAll('[data-right]').forEach(x=>{const r=x.dataset.rightRole,k=x.dataset.right;if(x.checked&&body[r]&&k!=='view')body[r].push(k)});
 try{await postJSON('/api/accounts/permissions',body);accountMsg('Rights saved.',true);loadAccounts()}catch(e){accountMsg('Rights not saved: '+e.message)}});
$('disableOperator').addEventListener('click',async()=>{
 if(!confirm('Turn off ALL Operator logins? No Operator will be able to sign in until you add one again.'))return;
 try{await postJSON('/api/accounts/operator/disable',{});accountMsg('Operator login turned off.',true);loadAccounts()}catch(e){accountMsg('Not changed: '+e.message)}});
$('save').addEventListener('click',async()=>{const body={};KEYS.forEach(k=>{const el=$(k);body[k]=el.type==='checkbox'?el.checked:el.type==='number'?Number(el.value):el.value});body.alarm_days=Array.from(document.querySelectorAll('[data-day]:checked'),x=>Number(x.dataset.day));body.recipients=$('recipients').value.split(/[,\n]/).map(s=>s.trim()).filter(Boolean);body.sms_recipients=$('sms_recipients').value.split(/[,\n]/).map(s=>s.trim()).filter(Boolean);try{body.chemical_products=readProducts()}catch(e){$('saveState').textContent='Save failed: '+e.message;return}try{await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json','X-WW-Request':'1'},body:JSON.stringify(body)});$('saveState').textContent='Settings saved to server.';loadSummary();loadSettings()}catch(e){$('saveState').textContent='Save failed: '+e.message}});
function reportDownload(fmt){const a=$('from').value,b=$('to').value;if(!a||!b||a>b){alert('Choose a valid date range.');return}let days=(Date.parse(b+'T00:00:00Z')-Date.parse(a+'T00:00:00Z'))/86400000;if(days>91){alert('Maximum 92 inclusive days (one quarter)');return}window.location.href='/api/export?tag='+encodeURIComponent($('reportTag').value)+'&startDate='+encodeURIComponent(a)+'&endDate='+encodeURIComponent(b)+(fmt?'&format='+fmt:'')}
const vanNow=()=>{const p=Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:'America/Vancouver',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(new Date(Date.now()+clockOffset)).map(x=>[x.type,x.value]));return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`};
const pickupMsg=(t,ok)=>{const el=$('pickupState');el.textContent=t;el.className=ok?'ok-text':'err-text'};
async function loadPickups(){
 $('pickupForm').hidden=!can('pickup');$('pickupReport').hidden=!can('export');
 const now=vanNow();$('pickupWhen').max=now;if(!$('pickupWhen').value)$('pickupWhen').value=now;if(!$('pickupMonth').value)$('pickupMonth').value=now.slice(0,7);$('pickupMonth').max=now.slice(0,7);
 try{const d=await api('/api/pickups');const rows=d.rows||[];
  $('pickupList').innerHTML=`<div class="tablewrap"><table><thead><tr><th>Pickup (Vancouver)</th><th>Days since previous</th><th>Logged by</th><th>Note</th><th>Action</th></tr></thead><tbody>${rows.map((r,i)=>{const prev=rows[i+1];const gap=prev?((Date.parse(r.picked_at)-Date.parse(prev.picked_at))/864e5).toFixed(1):'—';
   return `<tr><td>${local(r.picked_at)}</td><td>${gap}</td><td>${escapeHTML(r.logged_by)}</td><td>${escapeHTML(r.note)||'—'}</td><td>${can('pickup')?`<button type="button" class="danger" data-pickup="${r.id}">Remove</button>`:'—'}</td></tr>`}).join('')||'<tr><td colspan="5">No sludge pickups logged yet.</td></tr>'}</tbody></table></div>`;
  document.querySelectorAll('[data-pickup]').forEach(b=>b.onclick=async()=>{if(!confirm('Remove this pickup from the log? Use this only to fix a mistake.'))return;
   try{await postJSON('/api/pickups/remove',{id:Number(b.dataset.pickup)});pickupMsg('Pickup removed.',true);loadPickups();loadSummary()}catch(e){pickupMsg('Not removed: '+e.message)}});
 }catch(e){$('pickupList').textContent='Unable to load the pickup log: '+e.message}}
$('logPickup').addEventListener('click',async()=>{const when=$('pickupWhen').value;if(!when){pickupMsg('Enter the pickup date and time.');return}
 try{await postJSON('/api/pickups',{when,note:$('pickupNote').value});$('pickupNote').value='';$('pickupWhen').value=vanNow();pickupMsg(`Pickup logged for ${when.replace('T',' ')}.`,true);loadPickups();loadSummary()}catch(e){pickupMsg('Not logged: '+e.message)}});
$('pickupExcel').addEventListener('click',()=>{const m=$('pickupMonth').value;const today=vanNow().slice(0,10);if(!m||m+'-01'>today){alert('Choose this month or an earlier month.');return}
 const [y,mo]=m.split('-').map(Number);let end=`${m}-${String(new Date(Date.UTC(y,mo,0)).getUTCDate()).padStart(2,'0')}`;if(end>today)end=today;
 window.location.href=`/api/export?tag=sludge&startDate=${m}-01&endDate=${end}`});
// ---- Sensor calibration log (Audit page) ----
const calMsg=(t,ok)=>{const el=$('calState');el.textContent=t;el.className=ok?'ok-text':'err-text'};
const calDay=d=>{try{return new Intl.DateTimeFormat('en-CA',{timeZone:'UTC',year:'numeric',month:'short',day:'numeric'}).format(new Date(d+'T00:00:00Z'))}catch{return d}};
let calMaxKB=700;
async function loadCalibrations(){
 $('calReport').hidden=!can('export');const today=vanNow().slice(0,10);$('calDate').max=today;if(!$('calDate').value)$('calDate').value=today;
 try{const d=await api('/api/calibrations');const rows=d.rows||[],sensors=d.sensors||{};calMaxKB=d.max_file_kb||700;$('calMax').textContent=calMaxKB;
  const st=d.status||{};
  $('calLast').innerHTML=Object.entries(sensors).map(([k,s])=>{const last=rows.find(r=>r.sensor===k);const days=last?Math.round((Date.parse(today)-Date.parse(last.calibrated_on))/864e5):null;const c=st[k]||{};
   const due=c.state==='overdue'?`<em class="bad">OVERDUE since ${escapeHTML(calDay(c.due))}</em>`:c.state==='soon'?`<em class="warn">Due ${escapeHTML(calDay(c.due))} (in ${c.days_left} day${c.days_left===1?'':'s'})</em>`:c.due?`<em class="good">Next due ${escapeHTML(calDay(c.due))} (every ${c.every_days} days)</em>`:'';
   return `<div class="${escapeHTML(c.state||'')}"><b>${escapeHTML(s.label)}</b><span>${last?`Last calibrated ${escapeHTML(calDay(last.calibrated_on))} (${days} day${days===1?'':'s'} ago) by ${escapeHTML(last.calibrated_by)}`:'No calibration logged yet'}</span>${due}</div>`}).join('');
  $('calList').innerHTML=`<div class="tablewrap"><table><thead><tr><th>Sensor</th><th>Date calibrated</th><th>Calibrated by</th><th>Certificate no.</th><th>Certificate file</th><th>Note</th><th>Logged by</th><th>Action</th></tr></thead><tbody>${rows.map(r=>
   `<tr><td>${escapeHTML((sensors[r.sensor]||{}).label||r.sensor)}</td><td>${escapeHTML(calDay(r.calibrated_on))}</td><td>${escapeHTML(r.calibrated_by)}</td><td>${escapeHTML(r.certificate_no)||'—'}</td>`+
   `<td>${r.file_name?`<a href="/api/calibrations/certificate?id=${r.id}">${escapeHTML(r.file_name)}</a> <small>(${Math.ceil((r.file_size||0)/1000)} KB)</small>`:'—'}</td><td>${escapeHTML(r.note)||'—'}</td><td>${escapeHTML(r.logged_by)}<br><small>${escapeHTML(local(r.logged_at))}</small></td>`+
   `<td><button type="button" class="danger" data-cal="${r.id}">Remove</button></td></tr>`).join('')||'<tr><td colspan="8">No calibrations logged yet.</td></tr>'}</tbody></table></div>`;
  document.querySelectorAll('[data-cal]').forEach(b=>b.onclick=async()=>{if(!confirm('Remove this calibration record and its certificate file? Use this only to fix a mistake.'))return;
   try{await postJSON('/api/calibrations/remove',{id:Number(b.dataset.cal)});calMsg('Calibration removed.',true);loadCalibrations()}catch(e){calMsg('Not removed: '+e.message)}});
 }catch(e){$('calList').textContent='Unable to load the calibration log: '+e.message}}
const readFile=f=>new Promise((ok,bad)=>{const r=new FileReader();r.onload=()=>ok(String(r.result).split(',')[1]||'');r.onerror=()=>bad(Error('could not read the file'));r.readAsDataURL(f)});
$('logCal').addEventListener('click',async()=>{const f=$('calFile').files[0];
 if(f&&f.size>calMaxKB*1000){calMsg(`Certificate file is too large (${Math.ceil(f.size/1000)} KB, max ${calMaxKB} KB). Scan or save it at a lower resolution.`);return}
 const btn=$('logCal');btn.disabled=true;
 try{const body={sensor:$('calSensor').value,date:$('calDate').value,name:$('calName').value,certificate:$('calCert').value,note:$('calNote').value};
  if(f)body.file={name:f.name,data:await readFile(f)};
  await postJSON('/api/calibrations',body);['calName','calCert','calNote','calFile'].forEach(id=>$(id).value='');calMsg('Calibration logged.',true);loadCalibrations()}
 catch(e){calMsg('Not logged: '+e.message)}finally{btn.disabled=false}});
$('calExcel').addEventListener('click',()=>{window.location.href='/api/export?tag=calibration'});
$('calCsv').addEventListener('click',()=>{window.location.href='/api/export?tag=calibration&format=csv'});
// ---- Chemical stock and consumption (Reports page) ----
let stockProducts={},stockLabels={};
const GAL=3.785411784;
const toLitres=(q,unit,key)=>unit==='L'?q:unit==='gal'?q*GAL:q*((stockProducts[key]||{}).container_l||0);
const stockMsg=(t,ok)=>{const el=$('stockState');el.textContent=t;el.className=ok?'ok-text':'err-text'};
async function loadStock(){
 $('stockForm').hidden=!can('pickup');$('consReport').hidden=!can('export');if(!$('stockWhen').value)$('stockWhen').value=vanNow();$('stockWhen').max=vanNow();
 if(!$('consYear').value)$('consYear').value=vanNow().slice(0,4);
 try{const d=await api('/api/stock');const st=d.status||{},rows=d.rows||[];stockProducts=d.products||{};stockLabels=d.labels||{};fillProductLists(stockProducts);
  $('stockCards').innerHTML=Object.values(st).map(s=>{const head=`<b>${escapeHTML(s.name||s.label)}</b><span>${escapeHTML(s.container||'')} · Part # ${escapeHTML(s.part||'')}</span>`;
   if(s.litres==null)return `<div>${head}<span>No delivery or tank count logged yet</span></div>`;
   const low=s.low_l>0&&s.litres<s.low_l;const cont=s.container_l?s.litres/s.container_l:null;
   const left=s.days_left==null?'':s.order_now?`<em class="bad">ORDER NOW — about ${Math.floor(s.days_left)} days left, delivery takes ${s.lead_days} days</em>`:
    `<em class="${s.days_left<=s.lead_days+7?'warn':'good'}">About ${Math.floor(s.days_left)} days left · order by ${escapeHTML(calDay(s.order_by))}</em>`;
   return `<div class="${low||s.order_now?'overdue':s.days_left!=null&&s.days_left<=s.lead_days+7?'soon':'ok'}">${head}<b>${fmt(s.litres)} L${cont!=null?` (${fmt(cont)} × ${escapeHTML(s.container)})`:''}${low?' — LOW':''}</b>${left}`+
    `<span>${s.per_day!=null?`Using about ${fmt(s.per_day)} L/day (${s.rate_from==='pump'?'dosing pump, last 7 days':'from the tank counts'})`:s.tag?'Dosing pump: no usage in the last 7 days':'Log two tank counts at least a day apart to forecast the days left'}${s.low_l>0?` · low below ${fmt(s.low_l)} L`:''}</span>`+
    `<span>Since ${escapeHTML(local(s.since))}: start ${fmt(s.base)} L + delivered ${fmt(s.added)} L${s.tag?` − pump used ${fmt(s.used)} L`:''}</span></div>`}).join('');
  $('stockList').innerHTML=`<div class="tablewrap"><table><thead><tr><th>Time (Vancouver)</th><th>Chemical</th><th>Entry</th><th>Litres</th><th>Note</th><th>Logged by</th><th>Action</th></tr></thead><tbody>${rows.map(r=>
   `<tr><td>${escapeHTML(local(r.at))}</td><td>${escapeHTML(stockLabels[r.chemical]||r.chemical)}</td><td>${r.kind==='delivery'?'Delivery':'Tank count'}</td><td>${fmt(r.litres)}${(stockProducts[r.chemical]||{}).container_l?` <small>(${fmt(r.litres/stockProducts[r.chemical].container_l)} cont.)</small>`:''}</td><td>${escapeHTML(r.note)||'—'}</td><td>${escapeHTML(r.logged_by)}</td><td>${can('pickup')?`<button type="button" class="danger" data-stock="${r.id}">Remove</button>`:'—'}</td></tr>`).join('')||'<tr><td colspan="7">No chemical deliveries or tank counts logged yet.</td></tr>'}</tbody></table></div>`;
  document.querySelectorAll('[data-stock]').forEach(b=>b.onclick=async()=>{if(!confirm('Remove this stock entry? Use this only to fix a mistake.'))return;
   try{await postJSON('/api/stock/remove',{id:Number(b.dataset.stock)});stockMsg('Entry removed.',true);loadStock()}catch(e){stockMsg('Not removed: '+e.message)}});
 }catch(e){$('stockList').textContent='Unable to load chemical stock: '+e.message}}
$('logStock').addEventListener('click',async()=>{if(!$('stockLitres').value){stockMsg('Enter the quantity.');return}
 const litres=Math.round(toLitres(Number($('stockLitres').value),$('stockUnit').value,$('stockChem').value)*100)/100;
 try{await postJSON('/api/stock',{chemical:$('stockChem').value,kind:$('stockKind').value,litres,when:$('stockWhen').value,note:$('stockNote').value});
  ['stockLitres','stockNote'].forEach(id=>$(id).value='');$('stockWhen').value=vanNow();stockMsg('Entry logged.',true);loadStock()}catch(e){stockMsg('Not logged: '+e.message)}});
// ---- Chemical products (lists filled from Settings > Chemical products) ----
function fillProductLists(prods){document.querySelectorAll('[data-products]').forEach(sel=>{const keep=sel.value;
 sel.innerHTML=Object.values(prods).filter(p=>p.active!==false).map(p=>`<option value="${escapeHTML(p.key)}">${escapeHTML(p.name)} · ${escapeHTML(p.container)}${p.part?' · Part # '+escapeHTML(p.part):''}</option>`).join('');
 if(keep&&Array.from(sel.options).some(o=>o.value===keep))sel.value=keep})}
const MEASURED={'':'Tank counts','Coag_Pump_Run':'Coagulant dosing pump (Coag_Pump_Run)','Floc_Pump_Run':'Flocculant dosing pump (Floc_Pump_Run)'};
let productKeys=[];   // keys saved on the server; a new row gets a fresh key and can be deleted before it has records
function productRow(p){const v=x=>x==null?'':escapeHTML(String(x));const prices=p.prices||[];
 return `<tr data-key="${v(p.key)}"><td><input type="checkbox" data-f="active"${p.active!==false?' checked':''} aria-label="In use"></td>`+
  `<td><input data-f="name" maxlength="80" value="${v(p.name)}" aria-label="Product name"></td><td><input data-f="label" maxlength="40" value="${v(p.label)}" aria-label="Short name"></td>`+
  `<td><input data-f="part" maxlength="40" value="${v(p.part)}" aria-label="Part number"></td><td><input data-f="supplier" maxlength="80" value="${v(p.supplier)}" aria-label="Supplier"></td>`+
  `<td><input data-f="container" maxlength="40" value="${v(p.container)}" placeholder="55 gal drum" aria-label="Container"></td>`+
  `<td><input data-f="container_l" type="number" min="0.1" max="100000" step="0.001" value="${p.container_l==null?'':Math.round(p.container_l*1000)/1000}" aria-label="Litres per container"></td>`+
  `<td><select data-f="tag" aria-label="Measured by">${Object.entries(MEASURED).map(([k,t])=>`<option value="${k}"${(p.tag||'')===k?' selected':''}>${t}</option>`).join('')}</select></td>`+
  `<td><input data-f="low_l" type="number" min="0" max="100000" step="0.1" value="${v(p.low_l??0)}" aria-label="Low stock below litres"></td>`+
  `<td><input data-f="lead_days" type="number" min="0" max="365" step="1" value="${v(p.lead_days??7)}" aria-label="Delivery time days"></td>`+
  `<td><b class="cur-price" data-cur-price>${p.price!=null?money(p.price):'not set'}</b><button type="button" data-prices aria-expanded="false">Prices (${prices.length})</button></td>`+
  `<td><button type="button" class="danger" data-del-product>Delete</button></td></tr>`+
  `<tr class="price-row" data-prices-for="${v(p.key)}" hidden><td colspan="12"><b>Price history — ${v(p.name||'new product')}</b> `+
  `<small>Each price applies from its date until the next price. Costs, estimated purchase costs and reports use the price valid on each day, so earlier months keep their old price. To change the price, add a new line with the date it starts (do not overwrite the old one).</small>`+
  `<div class="price-list">${prices.map(priceEntry).join('')}</div><button type="button" data-add-price>Add price</button></td></tr>`}
function priceEntry(e={}){const v=x=>x==null?'':escapeHTML(String(x));
 return `<span class="price-entry"><label>From<input type="date" data-pf value="${v(e.from)}"></label><label>Price per container ($)<input type="number" data-pp min="0" max="1000000" step="0.01" value="${v(e.price)}"></label><button type="button" class="danger" data-del-price>Remove</button></span>`}
function renderProducts(items){productKeys=items.map(p=>p.key);$('productRows').innerHTML=items.map(productRow).join('');bindProductRows()}
const priceRowOf=tr=>$('productRows').querySelector(`tr[data-prices-for="${CSS.escape(tr.dataset.key)}"]`);
function bindProductRows(){
 document.querySelectorAll('[data-del-product]').forEach(b=>b.onclick=()=>{const tr=b.closest('tr');
  if(productKeys.includes(tr.dataset.key)&&!confirm('Delete this product? It is removed when you press Save settings (refused if it has purchases or tank counts).'))return;priceRowOf(tr)?.remove();tr.remove()});
 document.querySelectorAll('[data-prices]').forEach(b=>b.onclick=()=>{const row=priceRowOf(b.closest('tr'));row.hidden=!row.hidden;b.setAttribute('aria-expanded',String(!row.hidden))});
 document.querySelectorAll('[data-add-price]').forEach(b=>b.onclick=()=>{const list=b.parentElement.querySelector('.price-list');
  list.insertAdjacentHTML('beforeend',priceEntry({from:vanNow().slice(0,10)}));bindProductRows();list.lastElementChild.querySelector('[data-pp]').focus()});
 document.querySelectorAll('[data-del-price]').forEach(b=>b.onclick=()=>b.closest('.price-entry').remove())}
$('addProduct').addEventListener('click',()=>{const key='p'+Date.now().toString(36);$('productRows').insertAdjacentHTML('beforeend',productRow({key,active:true,lead_days:7,low_l:0}));bindProductRows();
 $('productRows').querySelector(`tr[data-key="${key}"] [data-f="name"]`).focus()});
function readProducts(){return Array.from(document.querySelectorAll('#productRows tr[data-key]'),tr=>{const f=n=>tr.querySelector(`[data-f="${n}"]`);const num=n=>f(n).value===''?null:Number(f(n).value);
 const name=f('name').value.trim();if(!name)throw Error('every chemical product needs a name');if(num('container_l')==null)throw Error(name+': enter the litres per container');
 if(!f('container').value.trim())throw Error(name+': enter the container (for example 55 gal drum)');
 const prices=Array.from(priceRowOf(tr).querySelectorAll('.price-entry'),e=>({from:e.querySelector('[data-pf]').value,price:e.querySelector('[data-pp]').value}))
  .filter(e=>e.from||e.price!=='').map(e=>{if(!e.from||e.price==='')throw Error(name+': each price needs a date and an amount');return {from:e.from,price:Number(e.price)}});
 return {key:tr.dataset.key,active:f('active').checked,name,label:f('label').value.trim(),part:f('part').value.trim(),supplier:f('supplier').value.trim(),container:f('container').value.trim(),
  container_l:num('container_l'),tag:f('tag').value,low_l:num('low_l')??0,lead_days:Math.round(num('lead_days')??7),prices}})}
// ---- Chemical cost per m³ (Reports page) ----
// Cost shown per m³ or per litre (1 m³ = 1000 L); the choice is kept in this browser.
let costUnit='m3';try{if(localStorage.getItem('ww.costUnit')==='L')costUnit='L'}catch{}
const perUnit=p=>{if(p.per_m3==null)return 'No discharge';const v=costUnit==='L'?p.per_m3/1000:p.per_m3;
 return '$'+v.toFixed(v>=1?2:v>=0.01?4:6)+(costUnit==='L'?' per litre':' per m³')};
let costData=null;
$('costUnit').value=costUnit;$('costUnit').onchange=()=>{costUnit=$('costUnit').value==='L'?'L':'m3';try{localStorage.setItem('ww.costUnit',costUnit)}catch{};if(costData)drawCost(costData)};
async function loadCost(){try{costData=await api('/api/cost');drawCost(costData)}catch(e){$('costCards').textContent='Unable to load the chemical cost: '+e.message}}
function drawCost(d){
 $('costCards').innerHTML=d.priced?d.periods.map(p=>`<div><b>${escapeHTML(p.label)}</b><b class="cost-big">${perUnit(p)}</b>`+
  `<span>${money(p.cost)} of chemicals for ${costUnit==='L'?fmt(p.m3*1000)+' L':fmt(p.m3)+' m³'} discharged</span>`+
  (p.products.length?`<span>${p.products.map(x=>`${escapeHTML(x.label)}: ${fmt(x.litres)} L${x.cost!=null?' · '+money(x.cost):' · no price'}`).join('<br>')}</span>`:'<span>No chemical usage recorded</span>')+
  (p.unpriced.length?`<em class="warn">No price yet for ${escapeHTML(p.unpriced.join(', '))}: not in the cost</em>`:'')+`</div>`).join('')
  :'<div><b>No prices yet</b><span>Enter the price per container of each chemical under Settings → Chemical products to see the cost per m³ or per litre.</span></div>'}
// ---- Chemical purchases (Reports page) ----
const buyMsg=(t,ok)=>{const el=$('buyState');el.textContent=t;el.className=ok?'ok-text':'err-text'};
const money=v=>v==null?'—':'$'+Number(v).toLocaleString('en-CA',{minimumFractionDigits:2,maximumFractionDigits:2});
async function loadPurchases(){
 $('purchaseForm').hidden=!can('pickup');$('buyReport').hidden=!can('export');const now=vanNow();
 if(!$('buyDate').value)$('buyDate').value=now.slice(0,10);$('buyDate').max=now.slice(0,10);$('buyReceived').max=now;if(!$('buyYear').value)$('buyYear').value=now.slice(0,4);
 try{const d=await api('/api/purchases');const rows=d.rows||[],prod=d.products||{};Object.assign(stockProducts,prod);
  $('buyList').innerHTML=`<div class="tablewrap"><table><thead><tr><th>Purchased</th><th>Product</th><th>Quantity</th><th>Cost</th><th>PO / invoice</th><th>Supplier</th><th>Received</th><th>Logged by</th><th>Action</th></tr></thead><tbody>${rows.map(r=>{const p=prod[r.chemical]||{};
   const cont=p.container_l?r.litres/p.container_l:null;const unit=(p.container||'').includes('drum')?'drums':'pails';
   const cost=r.cost!=null?money(r.cost):r.est_cost!=null?money(r.est_cost)+' <small>(est.)</small>':'—';
   const recv=r.received_at?escapeHTML(local(r.received_at)):`<b class="warn-text">Not received yet</b>${can('pickup')?` <button type="button" data-receive="${r.id}">Mark received</button>`:''}`;
   return `<tr><td>${escapeHTML(calDay(r.purchased_on))}</td><td>${escapeHTML(p.name||r.chemical)}<br><small>Part # ${escapeHTML(p.part||'')}</small></td><td>${cont!=null?fmt(cont)+' '+unit:''}<br><small>${fmt(r.litres)} L</small></td>`+
    `<td>${cost}</td><td>${escapeHTML(r.po)||'—'}</td><td>${escapeHTML(r.supplier)||'—'}</td><td>${recv}</td><td>${escapeHTML(r.logged_by)}</td>`+
    `<td>${can('pickup')?`<button type="button" class="danger" data-buy="${r.id}">Remove</button>`:'—'}</td></tr>`}).join('')||'<tr><td colspan="9">No chemical purchases logged yet.</td></tr>'}</tbody></table></div>`;
  document.querySelectorAll('[data-buy]').forEach(b=>b.onclick=async()=>{if(!confirm('Remove this purchase? If it was received, it is also taken off the chemical stock. Use this only to fix a mistake.'))return;
   try{await postJSON('/api/purchases/remove',{id:Number(b.dataset.buy)});buyMsg('Purchase removed.',true);loadPurchases();loadStock()}catch(e){buyMsg('Not removed: '+e.message)}});
  document.querySelectorAll('[data-receive]').forEach(b=>b.onclick=async()=>{const when=prompt('Received date and time (YYYY-MM-DD HH:MM, Vancouver time):',vanNow().replace('T',' '));if(!when)return;
   try{await postJSON('/api/purchases/receive',{id:Number(b.dataset.receive),received:when.trim().replace(' ','T')});buyMsg('Purchase marked received and added to stock.',true);loadPurchases();loadStock()}catch(e){buyMsg('Not changed: '+e.message)}});
 }catch(e){$('buyList').textContent='Unable to load chemical purchases: '+e.message}}
$('logBuy').addEventListener('click',async()=>{if(!$('buyQty').value){buyMsg('Enter the quantity.');return}if(!$('buyDate').value){buyMsg('Enter the purchase date.');return}
 const litres=Math.round(toLitres(Number($('buyQty').value),$('buyUnit').value,$('buyChem').value)*100)/100;
 try{await postJSON('/api/purchases',{chemical:$('buyChem').value,purchased_on:$('buyDate').value,litres,cost:$('buyCost').value===''?null:Number($('buyCost').value),
   po:$('buyPo').value,supplier:$('buySupplier').value,received:$('buyReceived').value||null,note:$('buyNote').value});
  ['buyQty','buyCost','buyPo','buyReceived','buyNote'].forEach(id=>$(id).value='');buyMsg('Purchase logged.',true);loadPurchases();loadStock()}catch(e){buyMsg('Not logged: '+e.message)}});
$('buyExcel').addEventListener('click',()=>{window.location.href='/api/export?tag=purchases&year='+encodeURIComponent($('buyYear').value)});
$('buyCsv').addEventListener('click',()=>{window.location.href='/api/export?tag=purchases&format=csv&year='+encodeURIComponent($('buyYear').value)});
$('consExcel').addEventListener('click',()=>{window.location.href='/api/export?tag=consumption&year='+encodeURIComponent($('consYear').value)});
$('consCsv').addEventListener('click',()=>{window.location.href='/api/export?tag=consumption&format=csv&year='+encodeURIComponent($('consYear').value)});
// ---- Alarm shelving (Alarms page) ----
const shelfMsg=(t,ok)=>{const el=$('shelfState');el.textContent=t;el.className=ok?'ok-text':'err-text'};
async function loadShelves(){const manage=can('settings');$('shelfForm').hidden=!manage;
 try{const d=await api('/api/shelves');const sel=$('shelfKey');const keep=sel.value;
  sel.innerHTML=Object.entries(d.choices||{}).map(([k,l])=>`<option value="${escapeHTML(k)}">${escapeHTML(l)}</option>`).join('');if(keep)sel.value=keep;
  $('shelfMax').textContent=d.max_hours;$('shelfHours').max=d.max_hours;const act=d.active||[];
  $('shelfPanel').hidden=!manage&&!act.length;
  $('shelfList').innerHTML=act.length?`<div class="tablewrap"><table><thead><tr><th>Alarm</th><th>Shelved until</th><th>Reason</th><th>By</th><th>Action</th></tr></thead><tbody>${act.map(s=>
   `<tr><td>${escapeHTML((d.choices||{})[s.key]||s.key)}</td><td>${escapeHTML(local(s.until))}</td><td>${escapeHTML(s.reason)}</td><td>${escapeHTML(s.shelved_by)}</td><td>${manage?`<button type="button" data-unshelve="${escapeHTML(s.key)}">Unshelve</button>`:'—'}</td></tr>`).join('')}</tbody></table></div>`:'<p>No alarms are shelved.</p>';
  document.querySelectorAll('[data-unshelve]').forEach(b=>b.onclick=async()=>{try{await postJSON('/api/shelves/remove',{key:b.dataset.unshelve});shelfMsg('Alarm unshelved.',true);loadShelves();loadSummary()}catch(e){shelfMsg('Not changed: '+e.message)}});
 }catch(e){$('shelfList').textContent='Unable to load shelved alarms: '+e.message}}
$('shelveBtn').addEventListener('click',async()=>{try{await postJSON('/api/shelves',{key:$('shelfKey').value,hours:Number($('shelfHours').value),reason:$('shelfReason').value});
 $('shelfReason').value='';shelfMsg('Alarm shelved.',true);loadShelves();loadSummary()}catch(e){shelfMsg('Not shelved: '+e.message)}});
$('export').addEventListener('click',()=>reportDownload(''));$('exportCsv').addEventListener('click',()=>reportDownload('csv'));
// /switch refuses the current role so the browser re-prompts; a different login returns to '/'.
$('switchUser').addEventListener('click',()=>{window.location.href='/switch?from='+encodeURIComponent(userName||userRole)});
$('reload').onclick=()=>loadHistory(false);$('period').onchange=()=>loadHistory(false);
const dateparts=new Intl.DateTimeFormat('en-CA',{timeZone:'America/Vancouver',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());const dates=Object.fromEntries(dateparts.map(p=>[p.type,p.value]));$('from').value=$('to').value=`${dates.year}-${dates.month}-${dates.day}`;
$('toggleDiagram').onclick=()=>{const open=$('processPanel').classList.toggle('open');$('toggleDiagram').setAttribute('aria-expanded',open);$('toggleDiagram').textContent=open?'Hide process diagram':'Show process diagram'};
$('openAlarms').onclick=()=>view('events');
try{$('activeAlarms').open=localStorage.getItem('ww.alarmsOpen')==='1'}catch{}
$('activeAlarms').addEventListener('toggle',()=>{try{localStorage.setItem('ww.alarmsOpen',$('activeAlarms').open?'1':'0')}catch{};if(summaryData)drawCards()});setInterval(tickAges,5000);
loadSummary();setInterval(loadSummary,5000);setInterval(()=>{if(!$('trends').hidden)loadHistory()},30000);window.addEventListener('resize',()=>{if(historyData&&!$('trends').hidden)chartRows('historyCharts')});

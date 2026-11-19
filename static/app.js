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
let flowUnit='L/min';try{if(localStorage.getItem('ww.flowUnit')==='gal/min')flowUnit='gal/min'}catch{}
const flowValue=v=>v==null?v:(flowUnit==='gal/min'?v*GAL_PER_L:v);
const unitFor=(key,unit)=>key==='FLOW_RATE'?flowUnit:unit;
function setFlowUnit(u){flowUnit=u==='gal/min'?'gal/min':'L/min';try{localStorage.setItem('ww.flowUnit',flowUnit)}catch{};drawCards();if(historyData&&!$('trends').hidden)chartRows('historyCharts')}
function applyRole(){document.querySelectorAll('.accounts').forEach(p=>p.hidden=!can('accounts'));document.querySelectorAll('nav button').forEach(b=>{const need=VIEW_PERMS[b.dataset.view];b.hidden=!!need&&!can(need)});const open=document.querySelector('.view:not([hidden])');if(open&&VIEW_PERMS[open.id]&&!can(VIEW_PERMS[open.id]))view('overview')}
async function api(path,opts={}){const response=await fetch(path,{cache:'no-store',...opts});if(!response.ok)throw Error('HTTP '+response.status);return response.json()}
function ageText(stamp){if(!stamp)return 'No readings';const s=Math.max(0,Math.round((Date.now()+clockOffset-Date.parse(stamp))/1000));return 'Updated '+(s<60?s+' s':s<3600?Math.floor(s/60)+' min':Math.floor(s/3600)+' h '+Math.floor(s%3600/60)+' min')+' ago'}
function tickAges(){document.querySelectorAll('[data-stamp]').forEach(el=>el.textContent=ageText(el.dataset.stamp))}
function escapeHTML(x){return String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function view(name){if(VIEW_PERMS[name]&&!can(VIEW_PERMS[name]))name='overview';document.querySelectorAll('.view').forEach(n=>n.hidden=n.id!==name);document.querySelectorAll('nav button').forEach(n=>n.classList.toggle('selected',n.dataset.view===name));if(name==='events'){loadEvents();loadPickups()}if(name==='audit')loadAudit();if(name==='settings')loadSettings();if(name==='trends')loadHistory()}
document.querySelectorAll('nav button').forEach(b=>b.addEventListener('click',()=>view(b.dataset.view)));
function drawCards(){
 if(!summaryData)return;
 const lookup=Object.fromEntries(summaryData.tags.map(t=>[t.name,t]));
 $('cards').innerHTML=TAGS.map(([key,label,unit])=>{
   const item=lookup[key]||{};const status=item.status||'missing';const good=status==='current'&&item.value!=null;
   const pill=good?'LIVE':status.toUpperCase();
   const tip=`${label}: ${status} · ${item.stamp?local(item.stamp):'No records'}`;
   return `<div class="card ${good?'':'not-live'}" title="${escapeHTML(tip)}"><div class="card-icon" aria-hidden="true">${ICONS[key]||''}</div><div class="card-meta"><div class="name">${escapeHTML(label)}</div><span class="pill-row"><span class="live-pill ${escapeHTML(status)}"><i class="state-dot ${escapeHTML(status)}"></i>${escapeHTML(pill)}</span></span><small class="age" data-stamp="${escapeHTML(item.stamp||'')}">${escapeHTML(ageText(item.stamp))}</small></div><div class="card-divider" aria-hidden="true"></div><div class="value">${good?fmt(key==='FLOW_RATE'?flowValue(item.value):item.value):'--.--'}${key==='FLOW_RATE'?`<select class="unit unit-select" data-flow-unit aria-label="Discharge flow unit" title="Click to show litres or US gallons per minute"><option value="L/min"${flowUnit==='L/min'?' selected':''}>L/min</option><option value="gal/min"${flowUnit==='gal/min'?' selected':''}>gal/min</option></select>`:`<span class="unit">${unit}</span>`}</div></div>`;
 }).join('');
 document.querySelectorAll('[data-flow-unit]').forEach(s=>s.onchange=()=>setFlowUnit(s.value));
 const item=(key)=>lookup[key]||{};
 const set=(id,key,unit='')=>{const t=item(key);$(id).textContent=(t.status==='current'&&t.value!=null?fmt(t.value):'--.--')+(unit?' '+unit:'')};
 set('tankPh','pH_Reading');set('tankLevel','EQ_Tank_Level_Ft');{const t=item('FLOW_RATE');$('diagramFlow').textContent=(t.status==='current'&&t.value!=null?fmt(flowValue(t.value)):'--.--')+' '+flowUnit}
 {const ex=summaryData.extras||{};const show=(id,tag,unit,dec)=>{const r=ex[tag]||{};const ok=r.status==='current'&&r.value!=null;const el=$(id);el.textContent=(ok?Number(r.value).toFixed(dec):'--.'+'-'.repeat(dec))+' '+unit;el.classList.toggle('live',ok);el.title=`${r.label||tag} (${tag}): ${r.status||'missing'}${r.stamp?' · '+local(r.stamp):''}`};
  show('rakeSpeed','Rake_Speed_VFD','Hz',1);show('coagRate','Coag_Pump_Rate_Percent','%',1);show('coagRun','Coag_Pump_Run','ml/min',2);show('flocRate','Floc_Pump_Rate_Percent','%',1);show('flocRun','Floc_Pump_Run','ml/min',2);
  {const r=ex.SLUDGE_TANK_LEVEL||{};const known=r.status==='current'&&r.value!=null;const full=known&&Number(r.value)>=1;
   $('sludgeState').textContent=known?(full?'FULL':'NORMAL'):'--';$('sludgeTank').className='sludge-tank'+(full?' full':known?' normal':'');
   $('sludgeTank').title=`Sludge tank level switch (SLUDGE_TANK_LEVEL, PLC B3:0/13): ${known?(full?'FULL':'not full'):(r.status||'missing')}${r.stamp?' · since '+local(r.stamp):''}`;
   $('sludgeRibbon').hidden=!full;
   const pump=(id,tag)=>{const r=ex[tag]||{};const known=r.status==='current'&&r.value!=null;const on=known&&Number(r.value)!==0;
    for(const el of [$(id),$(id+'Pill')]){el.classList.toggle('on',on);el.classList.toggle('off',known&&!on);el.title=`${r.label||tag} (${tag}): ${known?(on?'RUNNING':'OFF'):(r.status||'missing')}${r.stamp?' · since '+local(r.stamp):''}`}
    $(id+'Pill').textContent=known?(on?'● RUNNING':'OFF'):'--'};
   pump('sumpPump','SUMP_PUMP_STATUS');pump('mixerPump','Mixer_Pump_Staus');
   {const last=summaryData.last_pickup,every=summaryData.pickup_every_days||3;$('lastPickup').textContent=last?local(last):'none logged';
    const due=last?Date.parse(last)+every*864e5:null;$('nextPickup').textContent=due?local(due):'—';$('pickupMask').classList.toggle('overdue',!!due&&Date.now()+clockOffset>due)}if(full)$('sludgeRibbon').textContent=`⚠ SLUDGE TANK FULL${r.stamp?' since '+local(r.stamp):''} · arrange a sludge pickup`}}
 const ph=item('pH_Reading');const limits=summaryData.limits||{low:5.8,high:9};const banner=$('banner');banner.className='banner';
 const allCurrent=summaryData.tags.every(x=>x.status==='current');
 const outside=ph.status==='current'&&ph.value!=null&&(ph.value<limits.low||ph.value>limits.high);
 const age=summaryData.data_age_seconds;const staleMin=limits.stale_minutes||15;
 if(age!=null&&age>staleMin*60){banner.classList.add('alert');banner.textContent=`NO NEW DATA for ${Math.floor(age/60)} minutes · check the Ewon, Talk2M and the collector`;}
 else if(outside){banner.classList.add('alert');banner.textContent=`pH ${fmt(ph.value)} outside ${fmt(limits.low)}–${fmt(limits.high)} · ${limits.enabled?'Alarm monitoring enabled':'Alarm monitoring disabled'}`;}
 else if(allCurrent){banner.classList.add('ok');banner.textContent=`LIVE API-BACKED READINGS · pH limits ${fmt(limits.low)}–${fmt(limits.high)} · Vancouver time`}
 else{banner.textContent='Telemetry warning: one or more readings are missing, invalid or stale — check the collector.'}
 const active=summaryData.active_alarms||[];const box=$('activeAlarms');box.hidden=!active.length;
 const unacked=active.filter(r=>r.state==='open').length;box.classList.toggle('unacked',unacked>0);
 $('activeSummary').textContent=`Active alarms: ${active.length}`+(unacked?` · ${unacked} not acknowledged`:' · all acknowledged')+' — click to '+(box.open?'hide':'show');
 $('activeList').innerHTML=active.map(r=>`<li><b>${escapeHTML(r.kind)}</b> — ${escapeHTML(r.detail)} <small>(${escapeHTML(local(r.stamp))}, ${escapeHTML(r.state)})</small></li>`).join('');
}
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
async function loadSummary(){try{summaryData=await api('/api/summary');userRole=summaryData.role;userName=summaryData.user||'';clockOffset=Date.parse(summaryData.now)-Date.now()||0;perms=new Set(summaryData.permissions||[]);$('role').textContent=userName?`${userRole} · ${userName}`:userRole;applyRole();drawCards()}catch(e){$('banner').textContent='Unable to load API-backed stored readings: '+e.message}}
async function loadHistory(overview=false){const hours=Number($('period').value);try{historyData=await api('/api/history?from='+encodeURIComponent(new Date(Date.now()-hours*3600000).toISOString())+'&to='+encodeURIComponent(new Date().toISOString()));chartRows('historyCharts')}catch(e){$('historyCharts').textContent='History unavailable: '+e.message}}
async function loadEvents(){try{const data=await api('/api/events');$('eventlist').innerHTML=`<div class="tablewrap"><table><thead><tr><th>Time</th><th>Event</th><th>Detail</th><th>Status</th><th>Action</th></tr></thead><tbody>${data.rows.map(r=>`<tr><td>${local(r.stamp)}</td><td>${escapeHTML(r.kind)}</td><td>${escapeHTML(r.detail)}</td><td>${escapeHTML(r.state)}</td><td>${r.state==='open'&&can('acknowledge')?`<button data-ack="${r.id}">Acknowledge</button>`:'—'}</td></tr>`).join('')||'<tr><td colspan="5">No recorded alarms yet.</td></tr>'}</tbody></table></div>`;document.querySelectorAll('[data-ack]').forEach(b=>b.onclick=async()=>{try{await api('/api/ack',{method:'POST',headers:{'Content-Type':'application/json','X-WW-Request':'1'},body:JSON.stringify({id:Number(b.dataset.ack)})});loadEvents()}catch(e){alert('Could not acknowledge: '+e.message)}})}catch(e){$('eventlist').textContent=e.message}}
async function loadAudit(){try{const data=await api('/api/audit');$('auditlist').innerHTML=`<div class="tablewrap"><table><thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Detail</th></tr></thead><tbody>${data.rows.map(r=>`<tr><td>${local(r.stamp)}</td><td>${escapeHTML(r.actor)}</td><td>${escapeHTML(r.action)}</td><td>${escapeHTML(r.detail)}</td></tr>`).join('')||'<tr><td colspan="4">No audit entries.</td></tr>'}</tbody></table></div>`}catch(e){$('auditlist').textContent='Admin required or audit unavailable: '+e.message}}
const KEYS=['ph_low','ph_high','alarms_enabled','email_enabled','email_recovery_enabled','alarm_confirm_samples','alarm_recovery_samples','alarm_on','alarm_off','daily_enabled','daily_time','monthly_enabled','monthly_day','monthly_time',
 'stale_alert_enabled','stale_alert_minutes','level_alarm_enabled','level_high_ft','flow_alarm_enabled','flow_low_lpm','flow_low_minutes','quarterly_enabled'];
const dayNames=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];$('alarmDays').innerHTML=dayNames.map((d,i)=>`<label><input type="checkbox" data-day="${i}"> ${d}</label>`).join('');
async function loadSettings(){if(can('accounts'))loadAccounts();try{let r=await api('/api/settings');config=r.settings;KEYS.forEach(k=>{if($(k))$(k)[$(k).type==='checkbox'?'checked':'value']=config[k]});document.querySelectorAll('[data-day]').forEach(x=>x.checked=config.alarm_days.includes(Number(x.dataset.day)));$('recipients').value=(config.recipients||[]).join('\n');$('smtp').textContent='SMTP credentials: '+(r.email_configured?'file present (delivery not verified)':'not configured — email will not send');document.querySelectorAll('#settings input:not([data-fixed]),#settings textarea,#settings button').forEach(el=>el.disabled=!can('settings'))}catch(e){$('saveState').textContent=e.message}}
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
const RIGHT_LABELS={pickup:'Log sludge pickups',export:'Download reports (Excel and CSV)',acknowledge:'Acknowledge alarms',settings:'View and change Settings (alarms, email reports)',audit:'View the Audit log'};
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
$('save').addEventListener('click',async()=>{const body={};KEYS.forEach(k=>{const el=$(k);body[k]=el.type==='checkbox'?el.checked:el.type==='number'?Number(el.value):el.value});body.alarm_days=Array.from(document.querySelectorAll('[data-day]:checked'),x=>Number(x.dataset.day));body.recipients=$('recipients').value.split(/[,\n]/).map(s=>s.trim()).filter(Boolean);try{await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json','X-WW-Request':'1'},body:JSON.stringify(body)});$('saveState').textContent='Settings saved to server.';loadSummary()}catch(e){$('saveState').textContent='Save failed: '+e.message}});
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
$('export').addEventListener('click',()=>reportDownload(''));$('exportCsv').addEventListener('click',()=>reportDownload('csv'));
// /switch refuses the current role so the browser re-prompts; a different login returns to '/'.
$('switchUser').addEventListener('click',()=>{window.location.href='/switch?from='+encodeURIComponent(userName||userRole)});
$('reload').onclick=()=>loadHistory(false);$('period').onchange=()=>loadHistory(false);
const dateparts=new Intl.DateTimeFormat('en-CA',{timeZone:'America/Vancouver',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());const dates=Object.fromEntries(dateparts.map(p=>[p.type,p.value]));$('from').value=$('to').value=`${dates.year}-${dates.month}-${dates.day}`;
$('toggleDiagram').onclick=()=>{const open=$('processPanel').classList.toggle('open');$('toggleDiagram').setAttribute('aria-expanded',open);$('toggleDiagram').textContent=open?'Hide process diagram':'Show process diagram'};
$('openAlarms').onclick=()=>view('events');
try{$('activeAlarms').open=localStorage.getItem('ww.alarmsOpen')==='1'}catch{}
$('activeAlarms').addEventListener('toggle',()=>{try{localStorage.setItem('ww.alarmsOpen',$('activeAlarms').open?'1':'0')}catch{};if(summaryData)drawCards()});setInterval(tickAges,5000);
loadSummary();setInterval(loadSummary,10000);setInterval(()=>{if(!$('trends').hidden)loadHistory()},30000);window.addEventListener('resize',()=>{if(historyData&&!$('trends').hidden)chartRows('historyCharts')});

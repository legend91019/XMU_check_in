'use strict';
const $ = id => document.getElementById(id);
const names = {present:'已签到',absent:'缺勤',late:'迟到',leave:'请假',unknown:'未知',pending:'待签到'};
const kinds = {number:'数字签到',radar:'雷达签到',qr:'二维码签到',unknown:'签到'};
const results = {confirmed:'已回查确认',unconfirmed:'结果待核实，暂不重发',submitting:'提交中 / 待核实',rejected:'平台拒绝提交'};
let snapshot = null, detailData = null, toastTimer;
function el(tag, cls, text) { const e=document.createElement(tag); if(cls)e.className=cls; if(text!==undefined)e.textContent=text; return e; }
function when(value) { if(!value)return '暂无记录'; const d=new Date(typeof value==='number'?value*1000:value); return Number.isNaN(d.getTime())?String(value):d.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}); }
function toast(text) { $('toast').textContent=text; $('toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').hidden=true,4500); }
async function api(path,method='GET',body) {const options={method,credentials:'same-origin',headers:{}};if(body!==undefined){options.headers['Content-Type']='application/json';options.body=JSON.stringify(body);}const r=await fetch('api/'+path,options);let data;try{data=await r.json();}catch{throw Error('服务暂不可用，请稍后重试');}if(!r.ok){if(r.status===401&&path!=='unlock'){$('unlock').hidden=false;$('app').hidden=true;}const text=typeof data.detail==='string'?data.detail:'请检查填写内容';throw Error(text);}return data;}
function totals(target,values) {target.replaceChildren();for(const key of ['present','absent','late','leave','unknown','pending']){if(key==='pending'&&!values.pending)continue;const span=el('span',key);span.append(el('strong','',String(values[key]||0)),document.createTextNode(names[key]));target.append(span);}}
const openedSemesters = new Set();
function courseTable(target, courses) {
  target.replaceChildren();
  if (!courses.length) { target.append(el('p','course-empty muted small','没有符合条件的课程记录。')); return; }
  const heading=el('div','course-row heading');heading.append(el('span','','课程'));
  for(const key of ['present','absent','late','leave','unknown'])heading.append(el('span','course-count',names[key]));
  heading.append(el('span'));target.append(heading);
  for(const c of courses){
    const row=el('button','course-row');row.type='button';row.setAttribute('aria-label',`${c.name}，缺勤 ${c.absent} 次，查看明细`);
    const info=el('div','course-info');info.append(el('div','course-name',c.name),el('div','course-meta',`${c.total} 条记录 · ${c.sync_complete?'接口记录已同步':'记录需核实'}`));row.append(info);
    for(const key of ['present','absent','late','leave','unknown']){const count=el('span','course-count '+(key==='absent'&&c[key]?'absent':''),String(c[key]));count.dataset.label=names[key];row.append(count);}
    row.append(el('span','course-arrow','›'));row.onclick=()=>openCourse(c.id);target.append(row);
  }
}
function renderCourses() {
  if(!snapshot)return;
  const q=$('search').value.trim().toLowerCase(), groups=snapshot.semester_groups||[];
  const current=groups.find(g=>g.is_current);
  $('current-semester').textContent=current?`${current.label} · ${current.courses.length} 门课程`:'暂无本学期课程';
  totals($('totals'),current?current.totals:{});
  $('empty').hidden=snapshot.courses.length>0;$('courses').hidden=!snapshot.courses.length;
  const matches=cs=>cs.filter(c=>c.name.toLowerCase().includes(q));
  courseTable($('courses'),current?matches(current.courses):[]);
  const history=$('semester-history');history.replaceChildren();
  const older=groups.filter(g=>!g.is_current&&(!q||matches(g.courses).length));
  if(older.length)history.append(el('h3','history-heading','其他学期'));
  for(const g of older){
    const details=el('details','semester-group');details.dataset.semester=g.key;details.open=Boolean(q)||openedSemesters.has(g.key);
    const summary=el('summary');summary.append(el('strong','',g.label),el('span','small muted',`${g.courses.length} 门课程 · ${g.totals.total} 条记录`));
    const inside=el('div','semester-content'), stats=el('div','totals'), list=el('div','course-list');totals(stats,g.totals);courseTable(list,matches(g.courses));inside.append(stats,list);details.append(summary,inside);
    details.ontoggle=()=>{if(!$('search').value.trim()){if(details.open)openedSemesters.add(g.key);else openedSemesters.delete(g.key);}};
    history.append(details);
  }
}
function render() {const d=snapshot,s=d.settings,st=d.state;$('connection').textContent=st.connected?'学校账号已连接':'尚未连接学校账号';$('connection').classList.toggle('online',st.connected);$('intro-copy').textContent=d.profile.name?`${d.profile.name}，这里汇总了你各门课程的出勤情况。`:'登录学校账号后，在这里查看每门课的签到记录。';$('monitor-label').textContent=s.enabled?'自动签到监控中':'自动签到已暂停';$('toggle-monitor').textContent=s.enabled?'暂停监控':'启动监控';$('auto-number').checked=s.auto_number;$('auto-radar').checked=s.auto_radar;$('wait-policy').textContent=s.wait_for_count>0?`等待其他 ${s.wait_for_count} 人签到后自动提交`:'检测到签到后直接处理';$('last-poll').textContent=st.last_poll?'最近检查 '+when(st.last_poll):'尚未检查';$('last-sync').textContent=st.busy?'正在同步各课程历史，请稍候…':st.last_sync?'最近同步 '+when(st.last_sync):'尚未同步';$('sync').disabled=st.busy;$('notice').hidden=!st.error;$('notice').textContent=st.error||'';const active=$('active-events');active.replaceChildren();for(const v of st.active){const r=el('div','active-event');r.append(el('strong','',v.course),el('span','small muted',kinds[v.kind]||'签到'),el('span','badge '+v.status,names[v.status]||'未知'),el('span','event-result',results[v.result]||v.result));active.append(r);}renderCourses();const logs=$('logs');logs.replaceChildren();for(const log of d.logs){const row=el('div','log-row '+log.level);row.append(el('time','',when(log.created_at)),el('span','',log.message));logs.append(row);}if(!d.logs.length)logs.append(el('p','small muted','连接学校账号后，这里会显示运行记录。'));}
async function refresh(){try{snapshot=await api('dashboard');$('unlock').hidden=true;$('app').hidden=false;render();}catch(e){if(!$('app').hidden)toast(e.message);}}
async function save(changes){try{await api('settings','PUT',changes);await refresh();}catch(e){toast(e.message);await refresh();}}
$('unlock-form').onsubmit=async e=>{e.preventDefault();const b=e.submitter;b.disabled=true;$('unlock-error').textContent='';try{await api('unlock','POST',{password:$('admin-password').value});$('admin-password').value='';await refresh();}catch(err){$('unlock-error').textContent=err.message;}finally{b.disabled=false;}};
$('logout').onclick=async()=>{try{await api('logout','POST');snapshot=null;$('app').hidden=true;$('unlock').hidden=false;}catch(e){toast(e.message);}};
for(const button of document.querySelectorAll('.close-dialog'))button.onclick=()=>button.closest('dialog').close();
function accountDialog(){if(snapshot)$('school-user').value=snapshot.profile.username||'';$('account-error').textContent='';$('account-dialog').showModal();}
$('open-account').onclick=accountDialog;$('empty-account').onclick=accountDialog;
$('account-form').onsubmit=async e=>{e.preventDefault();const b=e.submitter;b.disabled=true;b.textContent='正在连接学校平台…';$('account-error').textContent='';try{await api('account','POST',{username:$('school-user').value,password:$('school-password').value,cookie:$('school-cookie').value});$('school-password').value='';$('school-cookie').value='';$('account-dialog').close();toast('账号已连接，正在读取课程记录');await refresh();}catch(err){$('account-error').textContent=err.message;}finally{b.disabled=false;b.textContent='登录并同步课程';}};
$('toggle-monitor').onclick=()=>save({enabled:!snapshot.settings.enabled});
$('auto-number').onchange=e=>save({auto_number:e.target.checked});$('auto-radar').onchange=e=>save({auto_radar:e.target.checked});
$('sync').onclick=async()=>{try{const data=await api('sync','POST');toast(data.message);await refresh();}catch(e){toast(e.message);}};
$('search').oninput=renderCourses;
$('open-strategy').onclick=()=>{const s=snapshot.settings;$('wait-for-count').value=s.wait_for_count;$('poll-interval').value=s.poll_interval;$('history-hours').value=s.history_interval/3600;$('radar-mode').value=s.radar_mode;$('latitude').value=s.latitude??'';$('longitude').value=s.longitude??'';$('strategy-error').textContent='';$('strategy-dialog').showModal();};
$('strategy-form').onsubmit=async e=>{e.preventDefault();try{await api('settings','PUT',{wait_for_count:Number($('wait-for-count').value),poll_interval:Number($('poll-interval').value),history_interval:Math.round(Number($('history-hours').value)*3600),radar_mode:$('radar-mode').value,latitude:$('latitude').value===''?null:Number($('latitude').value),longitude:$('longitude').value===''?null:Number($('longitude').value)});$('strategy-dialog').close();toast('策略已保存');await refresh();}catch(err){$('strategy-error').textContent=err.message;}};
async function openCourse(id){try{detailData=await api('courses/'+encodeURIComponent(id));const c=detailData.course;$('detail-name').textContent=c.name;$('detail-notice').hidden=Boolean(c.sync_complete);$('detail-notice').textContent=c.sync_error||'本课程记录尚未完整核实，当前次数可能不完整。';totals($('detail-totals'),c);$('detail-filter').value='all';renderRecords();$('course-dialog').showModal();}catch(e){toast(e.message);}}
function renderRecords(){if(!detailData)return;const filter=$('detail-filter').value;const rows=detailData.records.filter(r=>filter==='all'||r.status===filter);$('detail-count').textContent=`显示 ${rows.length} / ${detailData.records.length} 条记录`;$('detail-records').replaceChildren();for(const v of rows){const r=el('div','record');const date=el('time','',when(v.occurred_at));if(v.occurred_at)date.dateTime=v.occurred_at;r.append(date,el('span','record-type',kinds[v.kind]||'签到'),el('span','badge '+v.status,names[v.status]||'未知'));$('detail-records').append(r);}if(!rows.length)$('detail-records').append(el('p','muted small','没有符合条件的记录。'));}
$('detail-filter').onchange=renderRecords;
refresh();setInterval(()=>{if(snapshot&&!document.hidden)refresh();},10000);

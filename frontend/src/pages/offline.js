import {book,openNotebook} from '../notebook.js';
import {api,account} from '../api.js';
import {headline,hiveName,intakeBadge} from '../components/notes.js';
import {INTAKE_STATES,OUTCOMES,VISIT_STATES} from '../constants.js';
import {text,num,fields,toast,when} from '../utils.js';

const local=date=>new Date(date).toISOString().slice(0,16);

// 离线批次：同一批次号重试不会重复入库，逐条结果可回查。
function composer(){
  const samples=[
    ['H-018','V-1117-201',24.1,31.2,52,.8,'true',7],
    ['H-031','V-1117-202',22.4,30.5,19,.2,'false',4],
  ];
  return `<section class="sheet"><h2>补传离线巡箱手记</h2>
  <form id="batch-form">
    <label>离线批次号<input name="client_batch_id" value="OFF-${Date.now().toString().slice(-8)}" required></label>
    <p class="hint">同批次号可放心重试；内容变化请换批次号。每条手记必须带现场采集时间。</p>
    <div id="rows">${samples.map((s,i)=>row(s,i)).join('')}</div>
    <div class="actions"><button type="button" id="add-row">再加一条手记</button><button class="primary" ${account().role==='viewer'?'disabled':''}>提交批次补传</button></div>
  </form>
  <div class="rule">采集时间早于当前读数的记录保留为“晚到”，不改当前箱况、不进入筛查。处置进行中补传的记录待闭环后再参与筛查。</div>
  </section>`;
}

function row(s,i){
  const [code,visit,weight,temp,activity,feed,queen,frames]=s;
  return `<fieldset class="offline-row" data-row="${i}"><legend>手记 ${i+1}</legend>
  <div class="two"><label>蜂箱编号<input name="hive_code" value="${code}" required></label><label>巡检编号<input name="code" value="${visit||'V-'+Date.now().toString().slice(-6)+i}" required></label></div>
  <div class="two"><label>现场采集时间<input name="collected_at" type="datetime-local" value="${visit?local(Date.now()-3600e3):local(Date.now())}" required></label><label>抬箱体重 / kg<input name="weight_kg" type="number" step="0.1" min="0.1" value="${weight}" required></label></div>
  <div class="three"><label>箱温 °C<input name="temperature" type="number" step="0.1" value="${temp}" required></label><label>活动 只/分<input name="activity" type="number" step="0.1" min="0" value="${activity}" required></label><label>余饲 kg<input name="feed_left" type="number" step="0.1" min="0" value="${feed}" required></label></div>
  <div class="two"><label>巡检王况<select name="queen_seen"><option value="true" ${queen==='true'?'selected':''}>见王</option><option value="false" ${queen==='false'?'selected':''}>未见王</option></select></label><label>蜂量 / 框<input name="colony_frames" type="number" min="0" max="20" value="${frames}" required></label></div>
  </fieldset>`;
}

function batchCard(b){
  const s=b.summary||{},r=b.results||[];
  return `<article class="sheet" data-batch="${text(b.client_batch_id)}"><span class="code">批次 ${text(b.client_batch_id)}</span>
  <h3>${r.length} 条手记 · ${text(b.submitted_by)}<br><span class="hint">${when(b.submitted_at)}</span></h3>
  <p class="hint">当前${s.accepted||0} · 晚到${s.late||0} · 待接纳${s.deferred||0} · 冲突${s.conflict||0} · 重复${s.duplicate||0} · 拒绝${s.rejected||0}</p>
  <table><thead><tr><th>手记</th><th>结局</th><th>去向</th></tr></thead><tbody>${r.map(row=>`<tr><td>${text(row.code)}</td><td>${text(OUTCOMES[row.outcome]||row.outcome)}<br><span class="hint">${text(row.message||'')}</span></td><td>${row.visit_id?`<button data-trace="${row.visit_id}">回查</button>`:'—'}</td></tr>`).join('')}</tbody></table>
  </article>`;
}

function conflictCard(c){
  const rows=(c.readings||[]).map(r=>{
    const v=book.visits.find(x=>x.id===r.visit_id);
    return `<tr><td><span class="code">${text(r.code)}</span></td><td>${num(r.weight_kg)} kg<br><span class="hint">${num(r.temperature,1)}°C · 活动${num(r.activity,0)} · 余饲${num(r.feed_left)}</span></td><td>${r.queen_seen?'见王':'未见王'} / ${r.colony_frames}框</td><td>${v?intakeBadge(v):''}</td><td>${c.state==='open'?`<input type="radio" name="pick-${c.id}" value="${r.visit_id}" ${account().role!=='admin'?'disabled':''}>`:(c.resolved_visit_id===r.visit_id?'<span class="badge screened">已选定</span>':'<span class="badge stale">未采用</span>')}</td></tr>`;
  }).join('');
  return `<article class="sheet conflict" data-conflict="${c.id}"><span class="code">冲突 ${c.id} · ${text(hiveName(c.hive_id))}</span>
  <h3>同一采集时刻 ${when(c.collected_at)} 出现不同读数 <span class="badge ${c.state==='open'?'needs_requeen':'screened'}">${c.state==='open'?'待主管裁决':'已裁决'}</span></h3>
  <table><thead><tr><th>手记</th><th>体重 / 环境</th><th>王况 / 框数</th><th>接纳状态</th><th>选定</th></tr></thead><tbody>${rows}</tbody></table>
  ${c.state==='open'?`<form class="resolve-form" data-id="${c.id}"><label>裁决依据<input name="note" minlength="2" required placeholder="以现场复读/蜂师复核为准"></label><button class="primary" ${account().role!=='admin'?'disabled':''}>主管选定一份读数</button></form><p class="hint">未裁决期间该箱暂停新筛查与建立处置，其他蜂箱照常。</p>`:`<p class="hint">${text(c.decided_by)} · ${when(c.decided_at)}<br>${text(c.note)}</p>`}
  </article>`;
}

export function render(){
  const open=book.conflicts.filter(c=>c.state==='open'),done=book.conflicts.filter(c=>c.state!=='open');
  const trace=book.trace;
  return headline('10 / OFFLINE SYNC','把晚到的纸本手记，写回它发生的时刻','补传手记以现场采集时间为准：晚到记录原样保留，不改当前箱况、不进入筛查。同箱同采集时刻读数不一致时隔离冲突，由主管选定。')+
  `<div class="resource-layout"><aside>${composer()}</aside><section>
    <section class="sheet"><h2>待裁决读数冲突 · ${open.length}</h2>${open.map(conflictCard).join('')||'<div class="empty">没有待裁决冲突。</div>'}</section>
    <h2>补传批次与逐条去向</h2>${book.batches.map(batchCard).join('')||'<div class="empty">还没有补传批次。</div>'}
    ${done.length?`<h2>已裁决冲突</h2>${done.map(conflictCard).join('')}`:''}
  </section></div>
  <section class="sheet" id="trace-sheet"><h2>去向回查</h2>${trace?traceCard(trace):'<p class="hint">点任意批次的“回查”，查看该条手记的采集时间、接纳状态、批次与冲突去向。</p>'}</section>`;
}

function traceCard(t){
  const v=t.visit,c=t.conflict;
  return `<div class="two"><div>${`<span class="code">${text(v.code)} · ${text(hiveName(v.hive_id))}</span>`}<h3>${text(t.label)}</h3><p class="hint">采集 ${when(v.collected_at)}<br>入库 ${when(v.recorded_at)}<br>筛查状态 ${text(VISIT_STATES[v.status]||v.status)}<br>接纳状态 ${text(INTAKE_STATES[v.intake_state]||'当前读数')}</p><p>${num(v.weight_kg)} kg · ${v.queen_seen?'见王':'未见王'} · ${v.colony_frames}框</p></div><div>${t.batch?`<h3>来源批次</h3><p class="hint">${text(t.batch.batch_id)} · ${text(t.batch.submitted_by)}<br>${when(t.batch.submitted_at)}<br>该条结局 ${text(OUTCOMES[t.record?.outcome]||t.record?.message||'—')}</p>`:'<h3>来源批次</h3><p class="hint">在线登记或升级前历史手记，无补传批次。</p>'}${c?`<p class="hint">冲突 ${c.id} · ${c.state==='open'?'待裁决':`已由${text(c.decided_by)}裁决`}${c.note?`<br>${text(c.note)}`:''}</p>`:''}</div></div>`;
}

function parseRows(form){
  const records=[...form.querySelectorAll('.offline-row')].map((el,i)=>{
    const get=n=>el.querySelector(`[name="${n}"]`).value;
    // datetime-local 是本地时间，转成带时区的 UTC 再入库，回显才不偏移。
    const collected=get('collected_at'),collected_at=collected?new Date(collected).toISOString():'';
    return {client_record_id:get('code')||('row-'+i),code:get('code').trim(),hive_code:get('hive_code').trim(),collected_at,
      weight_kg:Number(get('weight_kg')),temperature:Number(get('temperature')),activity:Number(get('activity')),feed_left:Number(get('feed_left')),
      queen_seen:get('queen_seen')==='true',colony_frames:Number(get('colony_frames'))};
  });
  return records;
}

export function bind(rerender){
  document.querySelector('#add-row').onclick=()=>{const rows=document.querySelectorAll('.offline-row').length;document.querySelector('#rows').insertAdjacentHTML('beforeend',row(['H-018','',24,31,40,.8,'true',6],rows));};
  document.querySelector('#batch-form').onsubmit=async e=>{
    e.preventDefault();
    const form=e.target;
    if(account().role==='viewer')return;
    try{
      const body={client_batch_id:form.elements.client_batch_id.value.trim(),records:parseRows(form)};
      const result=await api('/visits/sync-batches','POST',body);
      await openNotebook();rerender();
      const s=result.summary||{};
      toast(result.duplicate?'该批次已入库过，未重复入库':`补传完成：当前${s.accepted} / 晚到${s.late} / 待接纳${s.deferred} / 冲突${s.conflict} / 重复${s.duplicate}`);
    }catch(err){toast(err.message);}
  };
  document.querySelectorAll('.resolve-form').forEach(f=>{
    f.onsubmit=async e=>{
      e.preventDefault();
      const pick=document.querySelector(`input[name="pick-${f.dataset.id}"]:checked`);
      if(!pick)return toast('请先选定一份读数');
      try{const r=await api(`/visit-conflicts/${f.dataset.id}/resolve`,'POST',{visit_id:Number(pick.value),note:fields(f).note});await openNotebook();rerender();toast(r.message||'冲突已裁决');}catch(err){toast(err.message);}
    };
  });
  document.querySelectorAll('[data-trace]').forEach(b=>b.onclick=async()=>{try{book.trace=await api('/visits/'+b.dataset.trace+'/trace');rerender();document.querySelector('#trace-sheet').scrollIntoView({behavior:'smooth'});}catch(err){toast(err.message);}});
}

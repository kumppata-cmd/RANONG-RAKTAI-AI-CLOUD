"""Workflow Builder UI — V5.0 Feature 4 (No-Code)

หน้า /admin/workflows — สร้าง/แก้/ลบ/ทดสอบ workflow ผ่านฟอร์ม
ใช้ API: /admin/api/v2/workflows/*  (auth: X-Admin-Secret header หรือ cookie admin_secret)

แยกออกมาเป็นไฟล์ เพื่อไม่ให้ server.py โตขึ้นอีก
"""

WORKFLOW_UI_HTML = r"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Workflow Builder — RANONG RAKTAI AI</title>
<style>
:root{--bg:#1a1a2e;--card:#16213e;--card2:#0f3460;--fg:#e0e0e0;--muted:#888;--acc:#4fc3f7;--ok:#6bcb77;--warn:#ffd93d;--err:#ff6b6b;--pri:#e94560}
*{box-sizing:border-box}
body{font-family:-apple-system,"Segoe UI",sans-serif;margin:0;padding:16px;background:var(--bg);color:var(--fg)}
h1{color:var(--acc);font-size:1.4rem;margin:0 0 4px}
h2{font-size:1.05rem;margin:18px 0 8px;color:var(--warn)}
.sub{color:var(--muted);font-size:.85rem;margin-bottom:14px}
.stats{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px}
.stat{background:var(--card);padding:10px 16px;border-radius:8px;text-align:center;min-width:90px}
.stat .num{font-size:1.4rem;font-weight:700;color:var(--acc)}
.stat .label{font-size:.72rem;color:var(--muted)}
.card{background:var(--card);border-radius:8px;padding:14px;margin:10px 0;border-left:4px solid var(--acc)}
.card.off{border-left-color:var(--muted);opacity:.7}
.card .name{font-weight:700;font-size:1rem}
.card .meta{font-size:.78rem;color:var(--muted);margin-top:3px}
.chip{display:inline-block;background:var(--card2);padding:2px 8px;border-radius:4px;font-size:.78rem;margin:2px 4px 2px 0}
.btn{background:var(--pri);color:#fff;border:none;padding:6px 12px;border-radius:4px;cursor:pointer;font-size:.82rem;margin:2px}
.btn.sec{background:var(--card2)}
.btn.ok{background:#2e7d32}
.btn:hover{filter:brightness(1.15)}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:6px 0}
input,select,textarea{background:#0b1a33;color:var(--fg);border:1px solid #2a3b5e;border-radius:4px;padding:6px 8px;font-size:.85rem;min-width:0}
input[type=text],textarea{flex:1;min-width:140px}
label{font-size:.8rem;color:var(--muted)}
.form{background:var(--card);border-radius:8px;padding:14px;margin:10px 0;border:1px dashed #2a3b5e}
.cond,.act{background:#0b1a33;border-radius:6px;padding:8px;margin:6px 0}
.empty{text-align:center;padding:30px;color:#666}
.err{color:var(--err);font-size:.85rem;white-space:pre-line}
.hist{font-size:.78rem;font-family:monospace;color:#aab;padding:4px 0;border-bottom:1px solid #223}
.hist .m{color:var(--ok)}.hist .nm{color:var(--muted)}.hist .f{color:var(--err)}
#toast{position:fixed;bottom:16px;right:16px;background:#263;color:#fff;padding:10px 16px;border-radius:6px;display:none;z-index:9}
.help{font-size:.75rem;color:var(--muted);margin-top:4px}
@media(max-width:600px){.row{flex-direction:column;align-items:stretch}}
</style>
</head>
<body>
<h1>⚙️ Workflow Builder</h1>
<div class="sub">สร้างกฎอัตโนมัติโดยไม่ต้องแก้โค้ด — เมื่อ [เหตุการณ์] ถ้า [เงื่อนไข] ให้ [ทำ]</div>
<div class="stats" id="stats"></div>

<div class="row">
  <button class="btn" onclick="newForm()">➕ สร้าง Workflow</button>
  <button class="btn sec" onclick="loadAll()">🔄 โหลดใหม่</button>
  <button class="btn sec" onclick="seedExamples()">📦 ใส่ตัวอย่าง 3 รายการ</button>
</div>

<div id="form"></div>
<h2>📋 Workflows</h2>
<div id="list"><div class="empty">กำลังโหลด...</div></div>
<h2>🕘 ประวัติการทำงาน (ล่าสุด 30)</h2>
<div id="hist"><div class="empty">—</div></div>
<div id="toast"></div>

<script>
const SECRET = document.cookie.split('admin_secret=')[1]?.split(';')[0] || '';
const H = {'X-Admin-Secret': SECRET, 'Content-Type': 'application/json'};
const API = '/admin/api/v2/workflows';
let CAT = {triggers:{},operators:{},actions:{}};
let EDITING = null;

const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(m, bad){const t=document.getElementById('toast');t.textContent=m;t.style.background=bad?'#722':'#263';t.style.display='block';setTimeout(()=>t.style.display='none',2500)}
async function j(url, opt={}){const r=await fetch(url,{headers:H,...opt});const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.error||(d.errors||[]).join('\n')||r.status);return d}

async function loadAll(){
  try{
    const [cat, list, stats, hist] = await Promise.all([
      j(API+'/catalog'), j(API), j(API+'/stats'), j(API+'/history?limit=30')]);
    CAT = cat; renderStats(stats); renderList(list.workflows||[]); renderHist(hist.history||[]);
  }catch(e){document.getElementById('list').innerHTML='<div class="empty err">❌ '+esc(e.message)+'</div>'}
}
function renderStats(s){
  document.getElementById('stats').innerHTML = `
   <div class="stat"><div class="num">${s.workflows||0}</div><div class="label">Workflows</div></div>
   <div class="stat"><div class="num">${s.enabled||0}</div><div class="label">เปิดใช้</div></div>
   <div class="stat"><div class="num">${s.runs||0}</div><div class="label">รันทั้งหมด</div></div>
   <div class="stat"><div class="num">${s.matched||0}</div><div class="label">ตรงเงื่อนไข</div></div>
   <div class="stat"><div class="num" style="color:${s.actions_failed?'var(--err)':'var(--ok)'}">${s.actions_failed||0}</div><div class="label">Action ล้มเหลว</div></div>`;
}
function renderList(ws){
  if(!ws.length){document.getElementById('list').innerHTML='<div class="empty">ยังไม่มี workflow — กด ➕ สร้าง หรือ 📦 ใส่ตัวอย่าง</div>';return}
  document.getElementById('list').innerHTML = ws.map(w=>`
   <div class="card ${w.enabled?'':'off'}">
     <div class="name">${w.enabled?'🟢':'⚪'} ${esc(w.name)} <span class="chip">${esc(w.id)}</span></div>
     <div class="meta">${esc(w.description||'')}</div>
     <div style="margin-top:6px">
       <span class="chip">⚡ ${esc(CAT.triggers[w.trigger]?.label||w.trigger)}</span>
       ${(w.conditions||[]).map(c=>`<span class="chip">🔍 ${esc(c.field)} ${esc(CAT.operators[c.op]||c.op)} ${esc(c.value??'')}</span>`).join('')}
       ${(w.conditions||[]).length>1?`<span class="chip">${w.match_all?'AND':'OR'}</span>`:''}
       ${(w.actions||[]).map(a=>`<span class="chip" style="background:#1b3a2b">▶ ${esc(CAT.actions[a.type]?.label||a.type)}</span>`).join('')}
     </div>
     <div class="meta">รัน ${w.run_count||0} ครั้ง ${w.last_run?'• ล่าสุด '+esc(w.last_run.slice(0,16)):''}</div>
     <div style="margin-top:8px">
       <button class="btn sec" onclick="editForm('${w.id}')">✏️ แก้ไข</button>
       <button class="btn sec" onclick="toggle('${w.id}',${!w.enabled})">${w.enabled?'⏸ ปิด':'▶ เปิด'}</button>
       <button class="btn sec" onclick="testWf('${w.id}')">🧪 ทดสอบ</button>
       <button class="btn" onclick="del('${w.id}','${esc(w.name)}')">🗑 ลบ</button>
     </div>
   </div>`).join('');
}
function renderHist(h){
  if(!h.length){document.getElementById('hist').innerHTML='<div class="empty">ยังไม่มีประวัติ</div>';return}
  document.getElementById('hist').innerHTML = h.map(r=>`<div class="hist">${esc(r.timestamp.slice(5,19))} <b>${esc(r.workflow_name)}</b> [${esc(r.trigger)}] ${r.matched?'<span class="m">✔ match</span>':'<span class="nm">– skip</span>'} ran=${r.actions_run} ${r.actions_failed?'<span class="f">fail='+r.actions_failed+' '+esc(r.errors.join('; '))+'</span>':''}${r.dry_run?' (dry)':''}</div>`).join('');
}

// ── Form ──
function blank(){return {name:'',description:'',trigger:'document_indexed',match_all:true,enabled:true,conditions:[],actions:[]}}
function newForm(){EDITING=blank();renderForm()}
async function editForm(id){EDITING=await j(API+'/'+id);renderForm()}
function cancelForm(){EDITING=null;document.getElementById('form').innerHTML=''}
function fieldsFor(trig){return CAT.triggers[trig]?.fields||[]}
function renderForm(){
  const w=EDITING; if(!w)return;
  const trigOpts=Object.entries(CAT.triggers).map(([k,v])=>`<option value="${k}" ${w.trigger===k?'selected':''}>${esc(v.label)}</option>`).join('');
  const opOpts=(sel)=>Object.entries(CAT.operators).map(([k,v])=>`<option value="${k}" ${sel===k?'selected':''}>${esc(v)}</option>`).join('');
  const fOpts=(sel)=>fieldsFor(w.trigger).map(f=>`<option value="${f}" ${sel===f?'selected':''}>${f}</option>`).join('')+`<option value="__custom" ${!fieldsFor(w.trigger).includes(sel)&&sel?'selected':''}>อื่นๆ…</option>`;
  const actOpts=(sel)=>Object.entries(CAT.actions).map(([k,v])=>`<option value="${k}" ${sel===k?'selected':''}>${esc(v.label)}</option>`).join('');
  document.getElementById('form').innerHTML=`
  <div class="form">
    <h2 style="margin-top:0">${w.id?'✏️ แก้ไข '+esc(w.id):'➕ Workflow ใหม่'}</h2>
    <div class="row"><label>ชื่อ</label><input type="text" id="f_name" value="${esc(w.name)}" placeholder="เช่น บิล NIKKISO → แจ้ง LINE"></div>
    <div class="row"><label>คำอธิบาย</label><input type="text" id="f_desc" value="${esc(w.description||'')}"></div>
    <div class="row"><label>⚡ เมื่อ</label><select id="f_trig" onchange="EDITING.trigger=this.value;syncForm();renderForm()">${trigOpts}</select>
      <label>เงื่อนไขหลายข้อ:</label><select id="f_all"><option value="1" ${w.match_all?'selected':''}>ต้องครบทุกข้อ (AND)</option><option value="0" ${!w.match_all?'selected':''}>ข้อใดข้อหนึ่ง (OR)</option></select></div>
    <div class="help">field ที่ใช้ได้: ${fieldsFor(w.trigger).join(', ')||'—'}</div>

    <h2>🔍 ถ้า (เงื่อนไข)</h2>
    <div id="conds">${w.conditions.map((c,i)=>`
      <div class="cond row">
        <select onchange="condField(${i},this)">${fOpts(c.field)}</select>
        <input type="text" style="max-width:150px;${fieldsFor(w.trigger).includes(c.field)?'display:none':''}" id="cf_${i}" value="${esc(c.field)}" placeholder="field">
        <select id="co_${i}">${opOpts(c.op)}</select>
        <input type="text" id="cv_${i}" value="${esc(c.value??'')}" placeholder="ค่า">
        <button class="btn sec" onclick="syncForm();EDITING.conditions.splice(${i},1);renderForm()">✖</button>
      </div>`).join('')}</div>
    <button class="btn sec" onclick="syncForm();EDITING.conditions.push({field:fieldsFor(EDITING.trigger)[0]||'',op:'eq',value:''});renderForm()">+ เงื่อนไข</button>

    <h2>▶ ให้ (การกระทำ)</h2>
    <div id="acts">${w.actions.map((a,i)=>{
      const p=CAT.actions[a.type]?.params||{};
      return `<div class="act">
        <div class="row"><select onchange="syncForm();EDITING.actions[${i}].type=this.value;EDITING.actions[${i}].params={};renderForm()">${actOpts(a.type)}</select>
        <button class="btn sec" onclick="syncForm();EDITING.actions.splice(${i},1);renderForm()">✖</button></div>
        ${Object.entries(p).map(([k,hint])=>`<div class="row"><label style="min-width:80px">${k}</label><input type="text" id="ap_${i}_${k}" value="${esc(a.params?.[k]??'')}" placeholder="${esc(hint)}"></div>`).join('')}
        ${CAT.actions[a.type]?.phi_safe===false?'<div class="help">⚠️ action นี้ส่งข้อมูลออกนอกระบบ — อย่าใส่ชื่อผู้ป่วยใน message</div>':''}
      </div>`}).join('')}</div>
    <button class="btn sec" onclick="syncForm();EDITING.actions.push({type:'log_event',params:{}});renderForm()">+ การกระทำ</button>

    <div class="row" style="margin-top:14px">
      <button class="btn ok" onclick="save()">💾 บันทึก</button>
      <button class="btn sec" onclick="cancelForm()">ยกเลิก</button>
    </div>
    <div class="err" id="f_err"></div>
  </div>`;
}
function condField(i,sel){syncForm();if(sel.value==='__custom'){EDITING.conditions[i].field='';}else{EDITING.conditions[i].field=sel.value}renderForm()}
function syncForm(){
  const w=EDITING; if(!w)return;
  w.name=document.getElementById('f_name')?.value??w.name;
  w.description=document.getElementById('f_desc')?.value??w.description;
  w.match_all=(document.getElementById('f_all')?.value??'1')==='1';
  w.conditions.forEach((c,i)=>{
    const cf=document.getElementById('cf_'+i); if(cf&&cf.style.display!=='none')c.field=cf.value;
    c.op=document.getElementById('co_'+i)?.value??c.op;
    c.value=document.getElementById('cv_'+i)?.value??c.value;
  });
  w.actions.forEach((a,i)=>{
    a.params=a.params||{};
    Object.keys(CAT.actions[a.type]?.params||{}).forEach(k=>{const el=document.getElementById(`ap_${i}_${k}`);if(el)a.params[k]=el.value});
  });
}
async function save(){
  syncForm(); const w=EDITING; const e=document.getElementById('f_err'); e.textContent='';
  try{
    if(w.id) await j(API+'/'+w.id,{method:'PUT',body:JSON.stringify(w)});
    else await j(API,{method:'POST',body:JSON.stringify(w)});
    toast('บันทึกแล้ว'); cancelForm(); loadAll();
  }catch(er){e.textContent=er.message}
}
async function toggle(id,en){try{await j(API+'/'+id+'/toggle',{method:'POST',body:JSON.stringify({enabled:en})});loadAll()}catch(e){toast(e.message,1)}}
async function del(id,name){
  // ไม่ใช้ confirm() — ใช้ 2-click
  const b=event.target; if(b.dataset.armed!=='1'){b.dataset.armed='1';b.textContent='⚠️ กดอีกครั้งเพื่อลบ';setTimeout(()=>{b.dataset.armed='0';b.textContent='🗑 ลบ'},3000);return}
  try{await j(API+'/'+id,{method:'DELETE'});toast('ลบแล้ว');loadAll()}catch(e){toast(e.message,1)}
}
async function testWf(id){
  const w=await j(API+'/'+id);
  const ctx={}; fieldsFor(w.trigger).forEach(f=>{ctx[f]='';});
  (w.conditions||[]).forEach(c=>{ctx[c.field]=c.value});  // ค่าที่น่าจะ match
  const sample=prompt('ข้อมูลทดสอบ (JSON):',JSON.stringify(ctx));
  if(sample===null)return;
  try{const r=await j(API+'/'+id+'/test',{method:'POST',body:JSON.stringify({ctx:JSON.parse(sample)})});
    toast((r.result.matched?'✔ ตรงเงื่อนไข — จะรัน '+r.result.actions_run+' action':'– ไม่ตรงเงื่อนไข')+' (dry-run)');loadAll()}
  catch(e){toast(e.message,1)}
}
async function seedExamples(){try{const r=await j(API+'/seed',{method:'POST'});toast('เพิ่ม '+r.created+' รายการ');loadAll()}catch(e){toast(e.message,1)}}
loadAll();
</script>
</body></html>"""

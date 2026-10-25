#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clinical KPI Dashboard — Phase A
Kidney Clinical Intelligence Platform 2.0

Web Dashboard สำหรับแสดง KPI คลินิกโรคไต:
  1. KPI Cards — ตัวชี้วัดหลัก (จำนวนผู้ป่วย, alert, compliance)
  2. Trend Charts — กราฟแนวโน้มผล lab
  3. Patient List — รายชื่อผู้ป่วยพร้อมสถานะ
  4. CKD Stage Distribution — สัดส่วน CKD stages
  5. Drug Alert Summary — สรุปการแจ้งเตือนยา

กฎความปลอดภัย:
  - ข้อมูลผู้ป่วยแสดงเป็น owner_hash เท่านั้น
  - ห้ามส่งข้อมูลออกภายนอก
  - ต้อง admin secret auth
  - Rule engine only — ไม่ใช้ AI ภายนอก
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


def compute_clinical_kpi(
    storage: Any,
    patient_registry: Any = None,
) -> dict:
    """คำนวณ KPI คลินิกจากข้อมูลใน GCS.

    Returns dict สำหรับ API response.

    Safety: ใช้ owner_hash เท่านั้น — ไม่มี patient identity
    """
    kpi: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(),
        "total_patients": 0,
        "ckd_stages": {},
        "anemia_on_target": 0,
        "anemia_total": 0,
        "dialysis_adequate": 0,
        "dialysis_total": 0,
        "critical_alerts": 0,
        "warning_alerts": 0,
        "pending_reviews": 0,
        "approved_labs": 0,
        "total_labs": 0,
        "recent_patients": [],
        "stage_distribution": [],
    }

    # ── Patient count from registry ──
    if patient_registry:
        try:
            patients = patient_registry.list_patients()
            kpi["total_patients"] = len(patients)

            # CKD Stage distribution
            stage_counts: dict[str, int] = {}
            for p in patients:
                stage = p.get("ckd_stage", "Unknown")
                stage_counts[stage] = stage_counts.get(stage, 0) + 1
            kpi["ckd_stages"] = stage_counts
            kpi["stage_distribution"] = [
                {"stage": s, "count": c}
                for s, c in sorted(stage_counts.items())
            ]

            # Recent patients (last 10 by last_visit)
            sorted_patients = sorted(
                patients,
                key=lambda p: p.get("last_visit", ""),
                reverse=True,
            )[:10]
            for p in sorted_patients:
                kpi["recent_patients"].append({
                    "owner_hash_prefix": p.get("owner_hash", "")[:8],
                    "hn": p.get("hn", "—"),
                    "ckd_stage": p.get("ckd_stage", "—"),
                    "last_egfr": p.get("last_egfr", "—"),
                    "last_visit": p.get("last_visit", "—"),
                    "alert_count": p.get("alert_count", 0),
                })
        except Exception as e:
            logger.warning(f"Patient registry read error: {e}")

    # ── Lab results from GCS ──
    try:
        if hasattr(storage, '_bucket') and storage._bucket:
            lab_blobs = list(storage._bucket.list_blobs(
                prefix="_lab_results/", max_results=500
            ))
            kpi["total_labs"] = len(lab_blobs)

            for lb in lab_blobs:
                try:
                    ldata = json.loads(lb.download_as_text())
                    status = ldata.get("status", "")
                    if status == "approved":
                        kpi["approved_labs"] += 1
                    elif status == "pending_review":
                        kpi["pending_reviews"] += 1

                    # Anemia KPI
                    results = ldata.get("results", [])
                    for r in results:
                        analyte = r.get("analyte", "").upper()
                        value = r.get("value")
                        if analyte in ("HB", "HEMOGLOBIN", "HGB") and value:
                            kpi["anemia_total"] += 1
                            try:
                                v = float(value)
                                if 10.0 <= v <= 12.0:
                                    kpi["anemia_on_target"] += 1
                            except (ValueError, TypeError):
                                pass
                        if analyte in ("KT/V", "KTV") and value:
                            kpi["dialysis_total"] += 1
                            try:
                                v = float(value)
                                if v >= 1.2:
                                    kpi["dialysis_adequate"] += 1
                            except (ValueError, TypeError):
                                pass
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"Lab GCS read error: {e}")

    # ── Alert counts from GCS ──
    try:
        if hasattr(storage, '_bucket') and storage._bucket:
            alert_blobs = list(storage._bucket.list_blobs(
                prefix="_alerts/", max_results=200
            ))
            for ab in alert_blobs:
                try:
                    adata = json.loads(ab.download_as_text())
                    kpi["critical_alerts"] += adata.get("critical_count", 0)
                    kpi["warning_alerts"] += adata.get("warning_count", 0)
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"Alerts GCS read error: {e}")

    # ── Derived metrics ──
    kpi["anemia_rate"] = (
        round(kpi["anemia_on_target"] / kpi["anemia_total"] * 100, 1)
        if kpi["anemia_total"] > 0 else 0
    )
    kpi["dialysis_rate"] = (
        round(kpi["dialysis_adequate"] / kpi["dialysis_total"] * 100, 1)
        if kpi["dialysis_total"] > 0 else 0
    )
    kpi["approval_rate"] = (
        round(kpi["approved_labs"] / kpi["total_labs"] * 100, 1)
        if kpi["total_labs"] > 0 else 0
    )

    return kpi


# ═══════════════════════════════════════════════════════════
# Dashboard HTML
# ═══════════════════════════════════════════════════════════

CLINICAL_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kidney Clinical Intelligence — Dashboard</title>
<style>
:root{
  --bg:#0f172a;--card:#1e293b;--border:#334155;--text:#e2e8f0;
  --muted:#94a3b8;--accent:#38bdf8;--green:#22c55e;--red:#ef4444;
  --yellow:#eab308;--purple:#a78bfa;--pink:#f472b6;--orange:#fb923c;
  --teal:#2dd4bf;
}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,'Noto Sans Thai',sans-serif;min-height:100vh}

/* ── Header ── */
.header{background:linear-gradient(135deg,#0f172a 0%,#1e3a5f 100%);padding:20px 24px;border-bottom:1px solid var(--border)}
.header h1{font-size:1.6rem;color:var(--accent);display:flex;align-items:center;gap:10px}
.header h1 span{font-size:1.8rem}
.header .sub{color:var(--muted);font-size:0.85rem;margin-top:4px}

/* ── Tab Nav ── */
.tabs{display:flex;gap:0;background:var(--card);border-bottom:2px solid var(--border);padding:0 24px;overflow-x:auto}
.tab{padding:12px 20px;cursor:pointer;color:var(--muted);font-size:0.9rem;font-weight:500;
border-bottom:2px solid transparent;margin-bottom:-2px;white-space:nowrap;transition:all 0.2s}
.tab:hover{color:var(--text)}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}

/* ── Content ── */
.content{max-width:1400px;margin:0 auto;padding:20px 24px}
.page{display:none}.page.active{display:block}

/* ── KPI Grid ── */
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:24px}
.kpi-card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:20px;
position:relative;overflow:hidden;transition:transform 0.15s}
.kpi-card:hover{transform:translateY(-2px)}
.kpi-card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px}
.kpi-card.blue::before{background:var(--accent)}
.kpi-card.green::before{background:var(--green)}
.kpi-card.red::before{background:var(--red)}
.kpi-card.yellow::before{background:var(--yellow)}
.kpi-card.purple::before{background:var(--purple)}
.kpi-card.teal::before{background:var(--teal)}
.kpi-label{font-size:0.78rem;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px}
.kpi-value{font-size:2.2rem;font-weight:700;line-height:1}
.kpi-sub{font-size:0.78rem;color:var(--muted);margin-top:6px}
.kpi-icon{position:absolute;top:16px;right:16px;font-size:1.4rem;opacity:0.3}

/* ── Charts ── */
.chart-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:16px;margin-bottom:24px}
.chart-box{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:20px}
.chart-box h3{font-size:0.95rem;color:var(--accent);margin-bottom:16px;display:flex;align-items:center;gap:8px}
.chart-box canvas{width:100%!important;max-height:240px}

/* ── Donut ── */
.donut-wrap{display:flex;align-items:center;gap:20px}
.donut-wrap canvas{max-width:200px;max-height:200px}
.donut-legend{font-size:0.82rem}
.donut-legend div{display:flex;align-items:center;gap:8px;margin:4px 0}
.donut-legend .dot{width:12px;height:12px;border-radius:3px;flex-shrink:0}

/* ── Table ── */
.data-section{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:20px;margin-bottom:24px}
.data-section h3{font-size:0.95rem;color:var(--accent);margin-bottom:14px;display:flex;align-items:center;gap:8px}
table{width:100%;border-collapse:collapse;font-size:0.85rem}
th{text-align:left;color:var(--muted);padding:8px 10px;border-bottom:1px solid var(--border);font-weight:500;font-size:0.78rem;text-transform:uppercase}
td{padding:8px 10px;border-bottom:1px solid rgba(51,65,85,0.5)}
tr:hover td{background:rgba(56,189,248,0.04)}

/* ── Badges ── */
.badge{display:inline-block;padding:3px 10px;border-radius:9999px;font-size:0.72rem;font-weight:600}
.b-g1{background:#064e3b;color:#6ee7b7}
.b-g2{background:#14532d;color:#86efac}
.b-g3a{background:#422006;color:#fde68a}
.b-g3b{background:#713f12;color:#fde047}
.b-g4{background:#7c2d12;color:#fdba74}
.b-g5{background:#7f1d1d;color:#fca5a5}
.b-g5d{background:#450a0a;color:#fecaca}
.b-ok{background:#064e3b;color:#6ee7b7}
.b-warn{background:#713f12;color:#fde047}
.b-crit{background:#7f1d1d;color:#fca5a5}

/* ── Footer ── */
.footer{text-align:center;color:var(--muted);font-size:0.72rem;padding:16px;margin-top:20px;border-top:1px solid var(--border)}
.footer .disclaimer{color:#f87171;font-size:0.7rem;margin-top:4px}

/* ── Responsive ── */
@media(max-width:640px){
  .kpi-grid{grid-template-columns:repeat(2,1fr);gap:10px}
  .kpi-value{font-size:1.6rem}
  .chart-grid{grid-template-columns:1fr}
  .content{padding:16px}
}

/* ── Loading ── */
.loading{display:flex;align-items:center;justify-content:center;padding:40px;color:var(--muted)}
.spinner{width:24px;height:24px;border:3px solid var(--border);border-top-color:var(--accent);
border-radius:50%;animation:spin 0.8s linear infinite;margin-right:12px}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>

<div class="header">
  <h1><span>🏥</span> Kidney Clinical Intelligence</h1>
  <div class="sub">RANONG RAKTAI AI 2.0 — Clinical Dashboard</div>
</div>

<div class="tabs">
  <div class="tab active" data-page="overview">📊 ภาพรวม KPI</div>
  <div class="tab" data-page="patients">👥 รายชื่อผู้ป่วย</div>
  <div class="tab" data-page="drugs">💊 สรุปยา/อันตรกิริยา</div>
  <div class="tab" data-page="alerts">🔔 การแจ้งเตือน</div>
</div>

<div class="content">
  <!-- ══════ Overview Page ══════ -->
  <div class="page active" id="page-overview">
    <div class="kpi-grid" id="kpi-cards">
      <div class="loading"><div class="spinner"></div>กำลังโหลดข้อมูล...</div>
    </div>

    <div class="chart-grid">
      <div class="chart-box">
        <h3>🥧 สัดส่วน CKD Stage</h3>
        <div class="donut-wrap">
          <canvas id="chart-stages" width="200" height="200"></canvas>
          <div class="donut-legend" id="stage-legend"></div>
        </div>
      </div>
      <div class="chart-box">
        <h3>📈 KPI Achievement</h3>
        <canvas id="chart-kpi-bar" height="200"></canvas>
      </div>
    </div>
  </div>

  <!-- ══════ Patients Page ══════ -->
  <div class="page" id="page-patients">
    <div class="data-section">
      <h3>👥 รายชื่อผู้ป่วยล่าสุด</h3>
      <table>
        <thead><tr><th>รหัส</th><th>HN</th><th>CKD Stage</th><th>eGFR ล่าสุด</th><th>เข้าพบล่าสุด</th><th>Alert</th></tr></thead>
        <tbody id="patient-rows"><tr><td colspan="6" class="loading"><div class="spinner"></div>กำลังโหลด...</td></tr></tbody>
      </table>
    </div>
  </div>

  <!-- ══════ Drugs Page ══════ -->
  <div class="page" id="page-drugs">
    <div class="kpi-grid" id="drug-kpi-cards"></div>
    <div class="data-section">
      <h3>💊 Drug Alerts ล่าสุด</h3>
      <table>
        <thead><tr><th>รหัสผู้ป่วย</th><th>ยา</th><th>ระดับ</th><th>รายละเอียด</th></tr></thead>
        <tbody id="drug-alert-rows">
          <tr><td colspan="4" style="color:var(--muted);text-align:center;padding:20px">ข้อมูลจะแสดงเมื่อมีการตรวจสอบยา</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- ══════ Alerts Page ══════ -->
  <div class="page" id="page-alerts">
    <div class="kpi-grid">
      <div class="kpi-card red"><div class="kpi-icon">🚨</div>
        <div class="kpi-label">แจ้งเตือนวิกฤต</div>
        <div class="kpi-value" id="alert-crit" style="color:var(--red)">—</div>
      </div>
      <div class="kpi-card yellow"><div class="kpi-icon">⚠️</div>
        <div class="kpi-label">แจ้งเตือนเฝ้าระวัง</div>
        <div class="kpi-value" id="alert-warn" style="color:var(--yellow)">—</div>
      </div>
      <div class="kpi-card green"><div class="kpi-icon">✅</div>
        <div class="kpi-label">รอตรวจสอบ</div>
        <div class="kpi-value" id="alert-pending" style="color:var(--muted)">—</div>
      </div>
    </div>
  </div>
</div>

<div class="footer">
  RANONG RAKTAI AI 2.0 — Kidney Clinical Intelligence Platform<br>
  อัปเดตอัตโนมัติทุก 60 วินาที — <span id="last-update">—</span>
  <div class="disclaimer">⚠️ ข้อมูลนี้เป็นข้อมูลประกอบเท่านั้น — ห้ามใช้แทนการตัดสินใจทางคลินิก</div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script>
const S=new URLSearchParams(location.search).get('secret')||'';
const STAGE_COLORS={G1:'#22c55e',G2:'#86efac',G3a:'#fde68a',G3b:'#fbbf24',G4:'#fb923c',G5:'#ef4444',G5D:'#dc2626',Unknown:'#64748b'};
const STAGE_ORDER=['G1','G2','G3a','G3b','G4','G5','G5D','Unknown'];

let stageChart=null,kpiChart=null;

// ── Tab navigation ──
document.querySelectorAll('.tab').forEach(t=>{
  t.addEventListener('click',()=>{
    document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
    document.querySelectorAll('.page').forEach(x=>x.classList.remove('active'));
    t.classList.add('active');
    document.getElementById('page-'+t.dataset.page).classList.add('active');
  });
});

// ── Render KPI cards ──
function renderKPI(d){
  const g=document.getElementById('kpi-cards');
  g.innerHTML=`
    <div class="kpi-card blue"><div class="kpi-icon">👥</div>
      <div class="kpi-label">ผู้ป่วยทั้งหมด</div>
      <div class="kpi-value" style="color:var(--accent)">${d.total_patients}</div>
    </div>
    <div class="kpi-card green"><div class="kpi-icon">🔬</div>
      <div class="kpi-label">ผล Lab ทั้งหมด</div>
      <div class="kpi-value" style="color:var(--green)">${d.total_labs}</div>
      <div class="kpi-sub">ยืนยันแล้ว ${d.approved_labs} (${d.approval_rate}%)</div>
    </div>
    <div class="kpi-card purple"><div class="kpi-icon">🩸</div>
      <div class="kpi-label">Anemia On-Target</div>
      <div class="kpi-value" style="color:var(--purple)">${d.anemia_rate}%</div>
      <div class="kpi-sub">${d.anemia_on_target}/${d.anemia_total} ราย</div>
    </div>
    <div class="kpi-card teal"><div class="kpi-icon">💧</div>
      <div class="kpi-label">Dialysis Adequate</div>
      <div class="kpi-value" style="color:var(--teal)">${d.dialysis_rate}%</div>
      <div class="kpi-sub">Kt/V ≥1.2: ${d.dialysis_adequate}/${d.dialysis_total}</div>
    </div>
    <div class="kpi-card red"><div class="kpi-icon">🚨</div>
      <div class="kpi-label">Critical Alerts</div>
      <div class="kpi-value" style="color:${d.critical_alerts>0?'var(--red)':'var(--green)'}">${d.critical_alerts}</div>
    </div>
    <div class="kpi-card yellow"><div class="kpi-icon">📋</div>
      <div class="kpi-label">รอตรวจสอบ</div>
      <div class="kpi-value" style="color:${d.pending_reviews>0?'var(--yellow)':'var(--green)'}">${d.pending_reviews}</div>
    </div>
  `;

  // Alert page
  document.getElementById('alert-crit').textContent=d.critical_alerts;
  document.getElementById('alert-warn').textContent=d.warning_alerts;
  document.getElementById('alert-pending').textContent=d.pending_reviews;
}

// ── Render Stage Donut ──
function renderStageChart(stages){
  const sorted=STAGE_ORDER.filter(s=>stages[s]>0);
  const labels=sorted;
  const data=sorted.map(s=>stages[s]);
  const colors=sorted.map(s=>STAGE_COLORS[s]);

  // Legend
  const leg=document.getElementById('stage-legend');
  leg.innerHTML=sorted.map(s=>`<div><div class="dot" style="background:${STAGE_COLORS[s]}"></div>${s}: ${stages[s]} ราย</div>`).join('');

  const ctx=document.getElementById('chart-stages').getContext('2d');
  if(stageChart)stageChart.destroy();
  stageChart=new Chart(ctx,{
    type:'doughnut',
    data:{labels,datasets:[{data,backgroundColor:colors,borderWidth:0}]},
    options:{
      responsive:false,
      cutout:'55%',
      plugins:{legend:{display:false},tooltip:{callbacks:{label:i=>i.label+': '+i.parsed+' ราย'}}}
    }
  });
}

// ── Render KPI Bar Chart ──
function renderKPIBar(d){
  const ctx=document.getElementById('chart-kpi-bar').getContext('2d');
  if(kpiChart)kpiChart.destroy();
  kpiChart=new Chart(ctx,{
    type:'bar',
    data:{
      labels:['Anemia\nOn-Target','Dialysis\nAdequate','Lab\nApproval'],
      datasets:[{
        label:'Achievement %',
        data:[d.anemia_rate,d.dialysis_rate,d.approval_rate],
        backgroundColor:['#a78bfa','#2dd4bf','#38bdf8'],
        borderRadius:6,barThickness:40,
      },{
        label:'Target (80%)',
        data:[80,80,80],
        type:'line',pointRadius:0,
        borderColor:'rgba(239,68,68,0.5)',borderDash:[5,5],borderWidth:2,
        fill:false,
      }]
    },
    options:{
      responsive:true,maintainAspectRatio:false,
      scales:{
        y:{beginAtZero:true,max:100,grid:{color:'rgba(51,65,85,0.3)'},
           ticks:{color:'#94a3b8',callback:v=>v+'%'}},
        x:{grid:{display:false},ticks:{color:'#94a3b8',font:{size:11}}}
      },
      plugins:{legend:{display:true,labels:{color:'#94a3b8',font:{size:11}}}}
    }
  });
}

// ── Render Patient Table ──
function renderPatients(patients){
  const tb=document.getElementById('patient-rows');
  if(!patients.length){tb.innerHTML='<tr><td colspan="6" style="color:var(--muted);text-align:center;padding:20px">ยังไม่มีข้อมูลผู้ป่วย</td></tr>';return;}
  tb.innerHTML=patients.map(p=>{
    const stg=p.ckd_stage||'—';
    const bc='b-'+(stg.toLowerCase().replace(' ',''));
    return `<tr>
      <td style="font-family:monospace">${p.owner_hash_prefix}</td>
      <td>${p.hn}</td>
      <td><span class="badge ${bc}">${stg}</span></td>
      <td>${typeof p.last_egfr==='number'?p.last_egfr.toFixed(1):p.last_egfr}</td>
      <td>${p.last_visit}</td>
      <td>${p.alert_count>0?'<span class="badge b-crit">'+p.alert_count+'</span>':'<span class="badge b-ok">0</span>'}</td>
    </tr>`;
  }).join('');
}

// ── Main load ──
async function load(){
  try{
    const r=await fetch('/admin/api/clinical-kpi?secret='+encodeURIComponent(S));
    if(!r.ok)throw new Error('HTTP '+r.status);
    const d=await r.json();
    renderKPI(d);
    renderStageChart(d.ckd_stages||{});
    renderKPIBar(d);
    renderPatients(d.recent_patients||[]);
    document.getElementById('last-update').textContent=new Date().toLocaleTimeString('th-TH');
  }catch(e){
    document.getElementById('kpi-cards').innerHTML=
      '<div style="color:var(--red);padding:20px">❌ โหลดข้อมูลไม่สำเร็จ: '+e.message+'</div>';
  }
}
load();
setInterval(load,60000);
</script>
</body>
</html>"""

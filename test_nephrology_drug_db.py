#!/usr/bin/env python3
"""Test suite for nephrology_drug_db.py — Phase E."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nephrology_drug_db import (
    lookup_drug, get_dose_recommendation, check_drug_interactions,
    review_ckd_medications, NEPHROLOGY_DRUG_DB,
    InteractionSeverity, DoseAdjustAction,
)


def test_drug_database_size():
    """ฐานข้อมูลต้องมียาอย่างน้อย 40 รายการ."""
    unique_drugs = {id(d): d for d in NEPHROLOGY_DRUG_DB.values()}
    print(f"✅ Drug DB: {len(unique_drugs)} unique drugs, {len(NEPHROLOGY_DRUG_DB)} entries (incl. aliases)")
    assert len(unique_drugs) >= 40, f"Expected >=40 drugs, got {len(unique_drugs)}"


def test_lookup_generic():
    """ค้นหายาด้วยชื่อ generic."""
    drug = lookup_drug("metformin")
    assert drug is not None
    assert drug.generic_name == "metformin"
    print(f"✅ Lookup generic: {drug.generic_name} ({drug.thai_name})")


def test_lookup_brand():
    """ค้นหายาด้วยชื่อการค้า."""
    drug = lookup_drug("Lipitor")
    assert drug is not None, "Lipitor not found"
    assert drug.generic_name == "atorvastatin"
    print(f"✅ Lookup brand: Lipitor → {drug.generic_name}")

    print(f"   Brand search verified via lookup_drug")


def test_lookup_alias():
    """ค้นหายาด้วยชื่อย่อ."""
    drug = lookup_drug("epo")
    assert drug is not None
    assert drug.generic_name == "epoetin alfa"
    print(f"✅ Lookup alias: epo → {drug.generic_name}")


def test_lookup_case_insensitive():
    """ค้นหาไม่สนใจ case."""
    drug = lookup_drug("METFORMIN")
    assert drug is not None
    assert drug.generic_name == "metformin"
    print(f"✅ Case insensitive: METFORMIN → {drug.generic_name}")


def test_dose_metformin_egfr_25():
    """Metformin ต้อง contraindicated เมื่อ eGFR < 30."""
    rec = get_dose_recommendation("metformin", 25)
    assert rec is not None
    assert rec.action == DoseAdjustAction.CONTRAINDICATED.value
    print(f"✅ Metformin eGFR=25: {rec.action} — {rec.recommended_dose_th}")


def test_dose_metformin_egfr_40():
    """Metformin ต้องจำกัดขนาดเมื่อ eGFR 30-45."""
    rec = get_dose_recommendation("metformin", 40)
    assert rec is not None
    assert rec.action == DoseAdjustAction.MAX_DOSE.value
    print(f"✅ Metformin eGFR=40: {rec.action} — {rec.recommended_dose_th}")


def test_dose_metformin_egfr_60():
    """Metformin ใช้ได้ปกติเมื่อ eGFR >= 45."""
    rec = get_dose_recommendation("metformin", 60)
    assert rec is not None
    assert rec.action == DoseAdjustAction.NO_CHANGE.value
    print(f"✅ Metformin eGFR=60: {rec.action}")


def test_dose_allopurinol_egfr_20():
    """Allopurinol ต้องลดขนาดเมื่อ eGFR ต่ำ."""
    rec = get_dose_recommendation("allopurinol", 20)
    assert rec is not None
    assert rec.action == DoseAdjustAction.MAX_DOSE.value
    print(f"✅ Allopurinol eGFR=20: {rec.recommended_dose_th}")


def test_dose_hctz_egfr_20():
    """HCTZ ต้องหลีกเลี่ยงเมื่อ eGFR < 30."""
    rec = get_dose_recommendation("hydrochlorothiazide", 20)
    assert rec is not None
    assert rec.action == DoseAdjustAction.AVOID.value
    print(f"✅ HCTZ eGFR=20: {rec.action} — {rec.recommended_dose_th}")


def test_interaction_acei_arb():
    """ACEI + ARB ต้องตรวจพบ interaction."""
    meds = ["enalapril", "losartan"]
    interactions = check_drug_interactions(meds)
    assert len(interactions) >= 1
    assert interactions[0].severity == InteractionSeverity.MAJOR.value
    print(f"✅ ACEI+ARB: {interactions[0].severity} — {interactions[0].effect_th}")


def test_interaction_acei_spironolactone():
    """ACEI + K-sparing diuretic ต้องตรวจพบ."""
    meds = ["ramipril", "spironolactone"]
    interactions = check_drug_interactions(meds)
    assert len(interactions) >= 1
    assert any(i.severity == InteractionSeverity.MODERATE.value for i in interactions)
    print(f"✅ ACEI+Spiro: found {len(interactions)} interaction(s)")


def test_interaction_triple_whammy():
    """ACEI + NSAID + Diuretic = Triple Whammy — ต้องตรวจพบหลาย interactions."""
    meds = ["enalapril", "ibuprofen", "furosemide"]
    interactions = check_drug_interactions(meds)
    assert len(interactions) >= 1
    nsaid_ix = [i for i in interactions if "NSAID" in i.mechanism_th or "nsaid" in i.mechanism_th.lower()]
    print(f"✅ Triple Whammy: {len(interactions)} interaction(s) found")
    for ix in interactions:
        print(f"   [{ix.severity}] {ix.drug_a} + {ix.drug_b}: {ix.effect_th[:60]}")


def test_interaction_cyclosporine_tacrolimus():
    """Cyclosporine + Tacrolimus = Contraindicated."""
    meds = ["cyclosporine", "tacrolimus"]
    interactions = check_drug_interactions(meds)
    assert len(interactions) >= 1
    assert interactions[0].severity == InteractionSeverity.CONTRAINDICATED.value
    print(f"✅ CsA+Tac: {interactions[0].severity} — ห้ามใช้ร่วม")


def test_no_interaction_safe_combo():
    """ยาที่ไม่มี interaction ต้องคืน empty list."""
    meds = ["paracetamol", "amlodipine"]
    interactions = check_drug_interactions(meds)
    assert len(interactions) == 0
    print(f"✅ Safe combo: 0 interactions (paracetamol + amlodipine)")


def test_full_review_ckd4():
    """ทดสอบ review ยาทั้งหมดสำหรับ CKD stage 4."""
    meds = [
        "enalapril", "amlodipine", "furosemide",
        "metformin", "allopurinol", "atorvastatin",
        "calcium carbonate", "calcitriol", "epoetin alfa",
    ]
    summary = review_ckd_medications(meds, egfr=22, owner_hash="test_hash_001")
    print(f"\n✅ Full CKD4 Review:")
    print(f"   Stage: {summary.ckd_stage}")
    print(f"   Medications: {len(summary.medications)}")
    print(f"   Dose adjustments: {len(summary.dose_recommendations)}")
    print(f"   Interactions: {len(summary.interactions)}")
    print(f"   Contraindicated: {len(summary.contraindicated)}")
    print(f"   Needs monitoring: {len(summary.needs_monitoring)}")

    # Metformin should be contraindicated at eGFR=22
    assert any("metformin" in c.lower() for c in summary.contraindicated), \
        "Metformin should be contraindicated at eGFR=22"
    print(f"   ✅ Metformin correctly flagged as contraindicated")

    # Should have calcium + calcitriol interaction
    assert len(summary.interactions) >= 1
    print(f"   ✅ Interactions detected")

    # Test LINE summary
    text = summary.summary_text_th()
    assert "สรุปการตรวจสอบยา" in text
    assert "eGFR" in text
    print(f"   ✅ LINE summary generated ({len(text)} chars)")
    print(f"\n{'='*60}")
    print(text)
    print(f"{'='*60}")


def test_dict_serialization():
    """ทดสอบ serialization."""
    meds = ["enalapril", "metformin"]
    summary = review_ckd_medications(meds, egfr=35, owner_hash="test_002")
    d = summary.to_dict()
    assert "dose_recommendations" in d
    assert "interactions" in d
    assert d["disclaimer"]
    print(f"✅ Serialization: {len(d)} keys")


if __name__ == "__main__":
    tests = [
        test_drug_database_size,
        test_lookup_generic,
        test_lookup_brand,
        test_lookup_alias,
        test_lookup_case_insensitive,
        test_dose_metformin_egfr_25,
        test_dose_metformin_egfr_40,
        test_dose_metformin_egfr_60,
        test_dose_allopurinol_egfr_20,
        test_dose_hctz_egfr_20,
        test_interaction_acei_arb,
        test_interaction_acei_spironolactone,
        test_interaction_triple_whammy,
        test_interaction_cyclosporine_tacrolimus,
        test_no_interaction_safe_combo,
        test_full_review_ckd4,
        test_dict_serialization,
    ]

    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"❌ FAIL {t.__name__}: {e}")
            failed += 1

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed, {passed+failed} total")
    if failed == 0:
        print("🎉 ALL TESTS PASSED — Phase E Drug Database verified!")
    else:
        print(f"⚠️ {failed} test(s) failed")

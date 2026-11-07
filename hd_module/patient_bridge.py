"""HD Smart Monitoring — Patient Bridge (Read-Only).

Reads patient data from MasterPatientRegistry without writing back.
Provides synthetic test data for development.
"""

import hashlib
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ── Synthetic Test Data (30 fake patients) ────────────────

SYNTHETIC_PATIENTS = [
    {"hn": f"T{90001 + i:05d}",
     "hospital_hn": f"H{60000 + i}",
     "patient_name": name,
     "gender": gender,
     "vascular_access": access,
     "dry_weight": dw,
     "schedule_days": sched,
     "esa_drug": esa,
     "esa_dose": dose,
     "primary_diagnosis": "CKD-G5 on HD"}
    for i, (name, gender, access, dw, sched, esa, dose) in enumerate([
        ("นายทดสอบ หนึ่ง", "M", "AVF left", 55.0, "mon,wed,fri", "Epiao", "4000u"),
        ("นางทดสอบ สอง", "F", "AVF right", 48.0, "tue,thu,sat", "Hemax", "4000u"),
        ("นายทดสอบ สาม", "M", "AVG left", 62.0, "mon,wed,fri", "Epiao", "6000u"),
        ("นางทดสอบ สี่", "F", "Perm catheter", 50.0, "tue,thu,sat", "Hema-plus", "4000u"),
        ("นายทดสอบ ห้า", "M", "AVF left", 70.0, "mon,wed,fri", "Epiao", "4000u"),
        ("นางทดสอบ หก", "F", "AVF right", 45.0, "tue,thu,sat", "Hemax", "4000u"),
        ("นายทดสอบ เจ็ด", "M", "AVG right", 58.0, "mon,wed,fri", "Epiao", "8000u"),
        ("นางทดสอบ แปด", "F", "AVF left", 52.0, "tue,thu,sat", "Hema-plus", "4000u"),
        ("นายทดสอบ เก้า", "M", "Temp catheter", 65.0, "mon,wed,fri", "Epiao", "4000u"),
        ("นางทดสอบ สิบ", "F", "AVF left", 47.0, "tue,thu,sat", "Hemax", "6000u"),
        ("นายทดสอบ สิบเอ็ด", "M", "AVF right", 60.0, "mon,wed,fri", "Epiao", "4000u"),
        ("นางทดสอบ สิบสอง", "F", "AVG left", 44.0, "tue,thu,sat", "Hemax", "4000u"),
        ("นายทดสอบ สิบสาม", "M", "AVF left", 72.0, "mon,wed,fri", "Epiao", "6000u"),
        ("นางทดสอบ สิบสี่", "F", "Perm catheter", 49.0, "tue,thu,sat", "Hema-plus", "4000u"),
        ("นายทดสอบ สิบห้า", "M", "AVF left", 68.0, "mon,wed,fri", "Epiao", "4000u"),
        ("นางทดสอบ สิบหก", "F", "AVF right", 42.0, "tue,thu,sat", "Hemax", "4000u"),
        ("นายทดสอบ สิบเจ็ด", "M", "AVF left", 63.0, "mon,wed,fri", "Epiao", "4000u"),
        ("นางทดสอบ สิบแปด", "F", "AVG right", 51.0, "tue,thu,sat", "Hemax", "6000u"),
        ("นายทดสอบ สิบเก้า", "M", "AVF left", 59.0, "mon,wed,fri", "Epiao", "8000u"),
        ("นางทดสอบ ยี่สิบ", "F", "AVF left", 46.0, "tue,thu,sat", "Hema-plus", "4000u"),
        ("นายทดสอบ ยี่สิบเอ็ด", "M", "AVF right", 66.0, "mon,wed,fri", "Epiao", "4000u"),
        ("นางทดสอบ ยี่สิบสอง", "F", "Perm catheter", 43.0, "tue,thu,sat", "Hemax", "4000u"),
        ("นายทดสอบ ยี่สิบสาม", "M", "AVF left", 71.0, "mon,wed,fri", "Epiao", "6000u"),
        ("นางทดสอบ ยี่สิบสี่", "F", "AVF left", 48.5, "tue,thu,sat", "Hemax", "4000u"),
        ("นายทดสอบ ยี่สิบห้า", "M", "AVG left", 64.0, "mon,wed,fri", "Epiao", "4000u"),
        ("นางทดสอบ ยี่สิบหก", "F", "AVF right", 50.5, "tue,thu,sat", "Hema-plus", "4000u"),
        ("นายทดสอบ ยี่สิบเจ็ด", "M", "AVF left", 57.0, "mon,wed,fri", "Epiao", "4000u"),
        ("นางทดสอบ ยี่สิบแปด", "F", "Temp catheter", 44.5, "tue,thu,sat", "Hemax", "4000u"),
        ("นายทดสอบ ยี่สิบเก้า", "M", "AVF left", 69.0, "mon,wed,fri", "Epiao", "6000u"),
        ("นางทดสอบ สามสิบ", "F", "AVF left", 47.5, "tue,thu,sat", "Hemax", "4000u"),
    ])
]


def _hash_record(record: dict) -> str:
    """Generate SHA-256 hash of a patient record for change detection."""
    import json
    data = json.dumps(record, sort_keys=True, default=str)
    return hashlib.sha256(data.encode()).hexdigest()


class PatientBridge:
    """Read-only bridge to the existing MasterPatientRegistry.

    Never writes back to the source registry.
    """

    def __init__(self, master_registry=None):
        self._registry = master_registry
        self._cache: dict[str, dict] = {}

    def sync_patients_readonly(self) -> list[dict]:
        """Read all patients from the registry (read-only).

        Returns list of patient dicts suitable for HdPatientMapping.
        Does NOT write to the registry or any database.
        """
        patients = []

        if self._registry is None:
            logger.info("No master registry — using synthetic data")
            return list(SYNTHETIC_PATIENTS)

        try:
            # Access internal patient store (read-only)
            raw_patients = getattr(self._registry, '_patients', {})
            for hn, p in raw_patients.items():
                record = {
                    "hn": getattr(p, 'clinic_hn', hn) or hn,
                    "hospital_hn": getattr(p, 'hospital_hn', ''),
                    "patient_name": getattr(p, 'patient_name', ''),
                    "vascular_access": getattr(p, 'vascular_access', ''),
                    "dry_weight": getattr(p, 'dry_weight', None),
                    "schedule_days": getattr(p, 'schedule_days', ''),
                    "esa_drug": getattr(p, 'esa_drug', ''),
                    "esa_dose": getattr(p, 'esa_dose', ''),
                    "primary_diagnosis": "CKD-G5 on HD",
                }
                record["source_registry_hash"] = _hash_record(record)
                patients.append(record)
                self._cache[record["hn"]] = record
        except Exception as e:
            logger.error(f"Failed to read registry: {e}")

        return patients

    def get_patient_by_hn(self, hn: str) -> Optional[dict]:
        """Look up a single patient by HN (read-only)."""
        if hn in self._cache:
            return self._cache[hn]

        if self._registry is not None:
            try:
                p = self._registry.match_hn(hn)
                if p:
                    record = {
                        "hn": getattr(p, 'clinic_hn', hn) or hn,
                        "hospital_hn": getattr(p, 'hospital_hn', ''),
                        "patient_name": getattr(p, 'patient_name', ''),
                        "vascular_access": getattr(p, 'vascular_access', ''),
                        "dry_weight": getattr(p, 'dry_weight', None),
                        "schedule_days": getattr(p, 'schedule_days', ''),
                        "esa_drug": getattr(p, 'esa_drug', ''),
                        "esa_dose": getattr(p, 'esa_dose', ''),
                    }
                    self._cache[hn] = record
                    return record
            except Exception as e:
                logger.warning(f"Registry lookup error for HN {hn}: {e}")

        # Fallback to synthetic data
        for sp in SYNTHETIC_PATIENTS:
            if sp["hn"] == hn:
                return sp
        return None

    def check_duplicates(self, patients: list[dict]) -> list[str]:
        """Check for duplicate HNs in a patient list."""
        seen = set()
        dupes = []
        for p in patients:
            hn = p.get("hn", "")
            if hn in seen:
                dupes.append(hn)
            seen.add(hn)
        return dupes

    def get_synthetic_patients(self) -> list[dict]:
        """Return synthetic test patients (30 records)."""
        return list(SYNTHETIC_PATIENTS)

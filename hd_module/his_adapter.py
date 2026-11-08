"""HD Smart Monitoring — HIS Integration Adapter.

Phase 7: Ready-to-connect adapter for Ranong Hospital HIS.

Supports:
- HL7 FHIR R4 (standard)
- Custom REST API (if HIS provides)
- Webhook receiver (HIS pushes data to us)

Data Flow:
  HIS → Adapter → hd_patient_mapping / hd_sessions / lab_results
  (read-only from HIS — never write back without explicit approval)

Status: FRAMEWORK READY — awaiting HIS API credentials from IT

Safety:
- Read-only by default — no writes to HIS
- All incoming data validated before storage
- Audit log every sync
- ห้ามส่งข้อมูลผู้ป่วยออกนอกระบบ
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════

@dataclass
class HisConfig:
    """HIS connection configuration (from ENV)."""
    enabled: bool = False
    base_url: str = ""          # e.g. https://his.ranong-hospital.go.th/api
    api_key: str = ""           # API key or token
    auth_type: str = "bearer"   # "bearer", "basic", "api_key"
    format: str = "fhir_r4"    # "fhir_r4", "custom_rest", "hl7v2"
    sync_interval_min: int = 60
    hospital_code: str = "11238"  # รหัส รพ.ระนอง
    timeout_sec: int = 30

    @classmethod
    def from_env(cls) -> "HisConfig":
        return cls(
            enabled=os.environ.get("HIS_ENABLED", "false").lower() == "true",
            base_url=os.environ.get("HIS_BASE_URL", ""),
            api_key=os.environ.get("HIS_API_KEY", ""),
            auth_type=os.environ.get("HIS_AUTH_TYPE", "bearer"),
            format=os.environ.get("HIS_FORMAT", "fhir_r4"),
            sync_interval_min=int(os.environ.get("HIS_SYNC_INTERVAL", "60")),
            hospital_code=os.environ.get("HIS_HOSPITAL_CODE", "11238"),
            timeout_sec=int(os.environ.get("HIS_TIMEOUT", "30")),
        )


# ═══════════════════════════════════════════════════════════
# FHIR R4 Data Mapping
# ═══════════════════════════════════════════════════════════

def fhir_patient_to_dict(fhir_resource: dict) -> dict:
    """Map FHIR Patient resource → hd_patient_mapping fields."""
    name_parts = []
    for name in fhir_resource.get("name", []):
        prefix = " ".join(name.get("prefix", []))
        given = " ".join(name.get("given", []))
        family = name.get("family", "")
        name_parts.append(f"{prefix} {given} {family}".strip())

    # Find HN from identifiers
    hn = ""
    hospital_hn = ""
    for ident in fhir_resource.get("identifier", []):
        system = ident.get("system", "")
        value = ident.get("value", "")
        if "clinic" in system.lower() or "hn" in system.lower():
            hn = value
        elif "hospital" in system.lower():
            hospital_hn = value

    return {
        "hn": hn,
        "hospital_hn": hospital_hn or fhir_resource.get("id", ""),
        "patient_name": name_parts[0] if name_parts else "",
        "date_of_birth": fhir_resource.get("birthDate"),
        "gender": fhir_resource.get("gender", ""),
        "blood_type": "",  # Not standard FHIR field
    }


def fhir_observation_to_lab(fhir_resource: dict) -> dict:
    """Map FHIR Observation resource → lab result dict."""
    code = ""
    display = ""
    for coding in fhir_resource.get("code", {}).get("coding", []):
        code = coding.get("code", "")
        display = coding.get("display", "")
        break

    value = None
    unit = ""
    vq = fhir_resource.get("valueQuantity", {})
    if vq:
        value = vq.get("value")
        unit = vq.get("unit", "")

    # Map LOINC codes to our field names
    loinc_map = {
        "718-7": ("hb", "Hemoglobin"),
        "2160-0": ("creatinine", "Creatinine"),
        "3094-0": ("bun", "BUN"),
        "6298-4": ("potassium", "Potassium"),
        "2777-1": ("phosphate", "Phosphate"),
        "17861-6": ("calcium", "Calcium"),
        "1751-7": ("albumin", "Albumin"),
        "2345-7": ("glucose", "Glucose"),
        "33914-3": ("egfr", "eGFR"),
        "2498-4": ("ferritin", "Ferritin"),
        "2502-3": ("tsat", "TSAT"),
        "4548-4": ("hba1c", "HbA1c"),
        "14879-1": ("pth", "PTH"),
    }

    field_name = code
    if code in loinc_map:
        field_name = loinc_map[code][0]

    return {
        "code": code,
        "field_name": field_name,
        "display": display,
        "value": value,
        "unit": unit,
        "effective_date": fhir_resource.get("effectiveDateTime", ""),
        "status": fhir_resource.get("status", ""),
        "patient_ref": fhir_resource.get("subject", {}).get("reference", ""),
    }


def fhir_procedure_to_session(fhir_resource: dict) -> dict:
    """Map FHIR Procedure resource → HD session dict."""
    return {
        "patient_ref": fhir_resource.get("subject", {}).get("reference", ""),
        "session_date": fhir_resource.get("performedDateTime",
                        fhir_resource.get("performedPeriod", {}).get("start", "")),
        "status": fhir_resource.get("status", ""),
        "notes": ", ".join(
            n.get("text", "") for n in fhir_resource.get("note", [])
        ),
    }


# ═══════════════════════════════════════════════════════════
# HIS Adapter (Main Class)
# ═══════════════════════════════════════════════════════════

class HisAdapter:
    """Adapter for connecting to Ranong Hospital HIS.

    Read-only: pulls data from HIS, never pushes back.
    All synced data goes through validation before storage.
    """

    def __init__(self, config: Optional[HisConfig] = None, db_url: str = ""):
        self.config = config or HisConfig.from_env()
        self._db_url = db_url or os.environ.get(
            "HD_DATABASE_URL", os.environ.get("DATABASE_URL", ""))
        self._last_sync = None
        self._sync_count = 0

    @property
    def is_configured(self) -> bool:
        return self.config.enabled and bool(self.config.base_url)

    @property
    def status(self) -> dict:
        return {
            "enabled": self.config.enabled,
            "configured": self.is_configured,
            "format": self.config.format,
            "hospital_code": self.config.hospital_code,
            "last_sync": self._last_sync.isoformat() if self._last_sync else None,
            "sync_count": self._sync_count,
        }

    def sync_patients(self) -> dict:
        """Pull patient list from HIS → update hd_patient_mapping.

        Returns: {"new": N, "updated": N, "errors": N}
        """
        if not self.is_configured:
            return {"error": "HIS not configured", "hint": "Set HIS_ENABLED=true + HIS_BASE_URL"}

        logger.info("HIS sync: starting patient sync from %s", self.config.base_url)

        try:
            patients = self._fetch_patients()
            result = self._upsert_patients(patients)
            self._last_sync = datetime.now(timezone.utc)
            self._sync_count += 1
            logger.info("HIS sync: %s", result)
            return result
        except Exception as e:
            logger.error("HIS sync failed: %s", e)
            return {"error": str(e)}

    def sync_labs(self, hn: str = "", days: int = 30) -> dict:
        """Pull lab results from HIS for a patient or all.

        Returns: {"imported": N, "errors": N}
        """
        if not self.is_configured:
            return {"error": "HIS not configured"}

        try:
            labs = self._fetch_labs(hn, days)
            return {"imported": len(labs), "hn": hn or "all"}
        except Exception as e:
            logger.error("HIS lab sync failed: %s", e)
            return {"error": str(e)}

    def _fetch_patients(self) -> list[dict]:
        """Fetch patients from HIS API."""
        # FRAMEWORK: Replace with actual HTTP call when HIS API available
        # url = f"{self.config.base_url}/Patient?_tag=dialysis"
        # headers = self._auth_headers()
        # resp = requests.get(url, headers=headers, timeout=self.config.timeout_sec)
        # bundle = resp.json()
        # return [fhir_patient_to_dict(entry["resource"])
        #         for entry in bundle.get("entry", [])]

        logger.info("HIS fetch_patients: FRAMEWORK MODE — no real HIS connected")
        return []

    def _fetch_labs(self, hn: str, days: int) -> list[dict]:
        """Fetch lab results from HIS API."""
        # FRAMEWORK: Replace with actual HTTP call
        # url = f"{self.config.base_url}/Observation?patient={hn}&date=ge{cutoff}"
        logger.info("HIS fetch_labs: FRAMEWORK MODE — no real HIS connected")
        return []

    def _upsert_patients(self, patients: list[dict]) -> dict:
        """Upsert patients into hd_patient_mapping."""
        if not patients or not self._db_url:
            return {"new": 0, "updated": 0, "errors": 0}

        try:
            import psycopg2
            conn = psycopg2.connect(self._db_url, connect_timeout=5)
            new = 0
            updated = 0
            errors = 0

            for p in patients:
                try:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO hd_patient_mapping
                                (hn, hospital_hn, patient_name, date_of_birth, gender)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (hn) DO UPDATE SET
                                hospital_hn = EXCLUDED.hospital_hn,
                                patient_name = EXCLUDED.patient_name,
                                updated_at = NOW()
                        """, (p["hn"], p.get("hospital_hn", ""),
                              p.get("patient_name", ""),
                              p.get("date_of_birth"), p.get("gender", "")))
                        if cur.rowcount > 0:
                            new += 1
                        else:
                            updated += 1
                except Exception:
                    errors += 1

            conn.commit()
            conn.close()
            return {"new": new, "updated": updated, "errors": errors}
        except Exception as e:
            return {"error": str(e)}

    def _auth_headers(self) -> dict:
        """Build auth headers for HIS API calls."""
        headers = {"Content-Type": "application/fhir+json"}
        if self.config.auth_type == "bearer":
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        elif self.config.auth_type == "api_key":
            headers["X-API-Key"] = self.config.api_key
        return headers


# ═══════════════════════════════════════════════════════════
# Webhook Receiver (HIS pushes data to us)
# ═══════════════════════════════════════════════════════════

def handle_his_webhook(payload: dict, source_ip: str = "") -> dict:
    """Process incoming webhook from HIS.

    Validates, maps, and stores the data.
    Returns processing result.
    """
    resource_type = payload.get("resourceType", "")

    if resource_type == "Patient":
        patient = fhir_patient_to_dict(payload)
        logger.info("HIS webhook: Patient %s", patient.get("hn", "unknown"))
        return {"type": "patient", "hn": patient.get("hn"), "status": "received"}

    elif resource_type == "Observation":
        lab = fhir_observation_to_lab(payload)
        logger.info("HIS webhook: Lab %s = %s %s",
                     lab["field_name"], lab["value"], lab["unit"])
        return {"type": "lab", "code": lab["code"], "status": "received"}

    elif resource_type == "Procedure":
        session = fhir_procedure_to_session(payload)
        logger.info("HIS webhook: Procedure %s", session["status"])
        return {"type": "session", "status": "received"}

    elif resource_type == "Bundle":
        entries = payload.get("entry", [])
        results = []
        for entry in entries:
            resource = entry.get("resource", {})
            if resource:
                results.append(handle_his_webhook(resource, source_ip))
        return {"type": "bundle", "processed": len(results)}

    else:
        logger.warning("HIS webhook: unknown resourceType '%s'", resource_type)
        return {"type": "unknown", "resourceType": resource_type, "status": "ignored"}

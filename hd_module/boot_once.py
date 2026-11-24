"""รันงานตอนบูต "ครั้งเดียวต่อเครื่อง (instance)" — 24 พ.ย. 2569 (แพทย์อนุมัติ)

ที่มา: gunicorn มี worker 2 ตัว และ worker ถูกสร้างใหม่เป็นระยะ ทุกครั้งที่ worker เกิด
มันจะ import server.py ทั้งไฟล์ → รัน migration (ALTER TABLE หลายสิบคำสั่ง), sync ผู้ป่วย,
สร้าง user ตั้งต้น ฯลฯ ซ้ำทุกครั้ง ใช้เวลา ~20–30 วิ ซึ่งระหว่างนั้น worker ตัวนั้นรับคำขอไม่ได้
(สาเหตุ ② ของเหตุ 502 วันที่ 24 พ.ย.)

วิธี: งานพวกนี้ idempotent อยู่แล้ว (CREATE/ALTER IF NOT EXISTS, skip ถ้ามีข้อมูล) จึงรัน
ครั้งเดียวต่อเครื่องพอ — worker แรกที่ได้ lock รัน แล้วเขียน marker ใน /dev/shm (RAM ของ
instance นั้น หายเมื่อ deploy ใหม่ → deploy ใหม่ก็รันใหม่ 1 ครั้ง) worker อื่น ๆ และ worker ที่
ถูกสร้างใหม่ภายหลังเห็น marker → ข้ามทันที

ความปลอดภัย:
- ถ้า /dev/shm ใช้ไม่ได้ (เช่น รัน local บน Windows) → รันงานตามปกติเหมือนเดิมทุกประการ
- HD_BOOT_ONCE=0 → ปิดกลไกนี้ (รันทุกครั้งเหมือนเดิม) ใช้เมื่อต้องการบังคับ migration ซ้ำ
- marker ผูกกับ commit ที่ deploy (RENDER_GIT_COMMIT) + เวอร์ชัน schema → โค้ดใหม่ = รันใหม่
- ถ้างานล้มเหลว (raise) จะไม่เขียน marker → worker ถัดไปลองใหม่ (พฤติกรรมเดิม: ลองทุกบูต)
"""
import logging
import os
import time

logger = logging.getLogger(__name__)

_ENV_FLAG = "HD_BOOT_ONCE"
_DIR_ENV = "HD_BOOT_MARK_DIR"


def _enabled():
    return os.environ.get(_ENV_FLAG, "1").strip().lower() not in ("0", "false", "no", "off")


def _mark_dir():
    d = os.environ.get(_DIR_ENV, "").strip() or "/dev/shm"
    return d if os.path.isdir(d) and os.access(d, os.W_OK) else None


def _tag(name, version):
    commit = (os.environ.get("RENDER_GIT_COMMIT", "") or "local")[:12]
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in f"{name}.{version}.{commit}")
    return safe


def run_once_per_instance(name, fn, version=""):
    """เรียก fn() ครั้งเดียวต่อ instance. คืน 'ran' | 'skipped' | 'ran_unguarded' | 'failed'.

    fn ต้องเป็นงาน idempotent. ข้อผิดพลาดใน fn ถูกจับและ log (ไม่ทำให้แอปเปิดไม่ขึ้น) —
    เหมือนพฤติกรรมเดิมของงานบูตทุกตัวที่ try/except ภายในอยู่แล้ว
    """
    d = _mark_dir()
    if not _enabled() or d is None:
        t0 = time.time()
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — งานบูตห้ามทำให้แอปล้ม
            logger.error("boot-once %s (unguarded) failed: %s", name, e)
            return "failed"
        logger.info("boot-once %s: ran (unguarded, %.1fs)", name, time.time() - t0)
        return "ran_unguarded"

    tag = _tag(name, version)
    marker = os.path.join(d, f"hd_boot_{tag}.done")
    lock_path = os.path.join(d, f"hd_boot_{tag}.lock")
    if os.path.exists(marker):
        logger.info("boot-once %s: skipped — already done on this instance (%s)", name, tag)
        return "skipped"

    fh = None
    try:
        import fcntl
        fh = open(lock_path, "w")
        fcntl.flock(fh, fcntl.LOCK_EX)          # worker ที่สองรอให้ตัวแรกทำเสร็จ แล้วค่อยเห็น marker
    except Exception as e:  # noqa: BLE001
        logger.warning("boot-once %s: lock unavailable (%s) — running unguarded", name, e)
        fh = None

    try:
        if os.path.exists(marker):
            logger.info("boot-once %s: skipped — done by another worker while waiting", name)
            return "skipped"
        t0 = time.time()
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            logger.error("boot-once %s failed: %s — will retry on next worker boot", name, e)
            return "failed"
        try:
            with open(marker, "w") as mf:
                mf.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} pid={os.getpid()}\n")
        except Exception as e:  # noqa: BLE001
            logger.warning("boot-once %s: could not write marker (%s)", name, e)
        logger.info("boot-once %s: ran (%.1fs) — later workers on this instance will skip", name, time.time() - t0)
        return "ran"
    finally:
        if fh is not None:
            try:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_UN)
                fh.close()
            except Exception:
                pass

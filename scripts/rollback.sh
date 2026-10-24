#!/bin/bash
# =============================================================================
# RANONG RAKTAI AI — Rollback Script
# สคริปต์สำหรับ rollback กลับไป commit ก่อนหน้า
#
# การใช้งาน: ./scripts/rollback.sh <commit-hash>
#
# ขั้นตอน:
#   1. ตรวจสอบว่า commit มีอยู่จริง
#   2. สร้าง rollback branch
#   3. Revert ไป commit ที่ระบุ
#   4. Push rollback branch
#   5. แสดงคำแนะนำสำหรับ Manual Deploy บน Render
# =============================================================================

set -euo pipefail

# สี (ถ้า terminal รองรับ)
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ===============================
# ตรวจ arguments
# ===============================
if [ $# -ne 1 ]; then
    echo -e "${RED}Error: ต้องระบุ commit hash${NC}"
    echo ""
    echo "การใช้งาน: $0 <commit-hash>"
    echo ""
    echo "ตัวอย่าง:"
    echo "  $0 abc1234"
    echo "  $0 abc1234567890abcdef1234567890abcdef12345"
    echo ""
    echo "ดู commit history:"
    echo "  git log --oneline -20"
    exit 1
fi

COMMIT_HASH="$1"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
ROLLBACK_BRANCH="rollback/${TIMESTAMP}"

echo "============================================="
echo " RANONG RAKTAI AI — Rollback"
echo "============================================="
echo ""

# ===============================
# 1. ตรวจว่าอยู่ใน git repo
# ===============================
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo -e "${RED}Error: ไม่ได้อยู่ใน git repository${NC}"
    exit 1
fi

# ===============================
# 2. ตรวจว่า commit มีอยู่จริง
# ===============================
echo -e "${BLUE}[1/5] ตรวจสอบ commit: ${COMMIT_HASH}${NC}"

if ! git cat-file -t "$COMMIT_HASH" > /dev/null 2>&1; then
    echo -e "${RED}Error: ไม่พบ commit ${COMMIT_HASH}${NC}"
    echo ""
    echo "ลอง:"
    echo "  git log --oneline -20    # ดู commits ล่าสุด"
    echo "  git fetch origin         # ดึง commits จาก remote"
    exit 1
fi

FULL_HASH=$(git rev-parse "$COMMIT_HASH")
COMMIT_MSG=$(git log -1 --format="%s" "$FULL_HASH")
COMMIT_DATE=$(git log -1 --format="%ci" "$FULL_HASH")

echo -e "  Commit: ${GREEN}${FULL_HASH:0:12}${NC}"
echo -e "  Message: ${COMMIT_MSG}"
echo -e "  Date: ${COMMIT_DATE}"
echo ""

# ===============================
# 3. ยืนยันก่อน rollback
# ===============================
echo -e "${YELLOW}WARNING: กำลังจะ rollback ไป commit ข้างบน${NC}"
echo -e "${YELLOW}branch ปัจจุบัน: $(git branch --show-current)${NC}"
echo ""
read -p "ต้องการดำเนินการต่อ? (y/N) " -n 1 -r
echo ""

if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "ยกเลิก rollback"
    exit 0
fi

# ===============================
# 4. สร้าง rollback branch
# ===============================
echo ""
echo -e "${BLUE}[2/5] สร้าง rollback branch: ${ROLLBACK_BRANCH}${NC}"
git checkout -b "$ROLLBACK_BRANCH"

# ===============================
# 5. Reset ไป commit ที่ระบุ
# ===============================
echo -e "${BLUE}[3/5] Reset ไป commit ${FULL_HASH:0:12}${NC}"
git reset --hard "$FULL_HASH"

# ===============================
# 6. Push rollback branch
# ===============================
echo -e "${BLUE}[4/5] Push rollback branch ไป remote${NC}"
git push -u origin "$ROLLBACK_BRANCH"

# ===============================
# 7. แสดงคำแนะนำ
# ===============================
echo ""
echo -e "${GREEN}=============================================${NC}"
echo -e "${GREEN} Rollback branch สร้างสำเร็จ!${NC}"
echo -e "${GREEN}=============================================${NC}"
echo ""
echo -e "${BLUE}[5/5] ขั้นตอนถัดไป — Manual Deploy บน Render:${NC}"
echo ""
echo "  1. ไปที่ Render Dashboard"
echo "     https://dashboard.render.com"
echo ""
echo "  2. เลือก service: ranong-raktai-ai"
echo ""
echo "  3. ไปที่ Settings → Branch"
echo "     เปลี่ยน branch เป็น: ${ROLLBACK_BRANCH}"
echo "     หรือ ไปที่ Manual Deploy → เลือก branch ${ROLLBACK_BRANCH}"
echo ""
echo "  4. กด 'Deploy' หรือ 'Manual Deploy'"
echo ""
echo "  5. ตรวจสอบหลัง deploy:"
echo "     - /health endpoint ต้องตอบ 200"
echo "     - LINE Bot webhook ต้องทำงาน"
echo "     - GCS connection ต้องเชื่อมต่อได้"
echo ""
echo "  6. ถ้า rollback สำเร็จ ให้ merge branch กลับ main:"
echo "     git checkout main"
echo "     git merge ${ROLLBACK_BRANCH}"
echo "     git push origin main"
echo ""
echo "============================================="
echo " Rollback commit: ${FULL_HASH:0:12}"
echo " Rollback branch: ${ROLLBACK_BRANCH}"
echo "============================================="

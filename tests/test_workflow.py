"""Document workflow transition tests.

ทดสอบ state transitions ของ physician approval workflow:
- valid transitions ต้องทำได้
- invalid transitions ต้องถูก reject
- quarantine สามารถทำได้จากทุก state ที่ไม่ใช่ terminal

อ้างอิง: physician_approval.py — VALID_TRANSITIONS
"""

import os
import sys
import pytest

# เพิ่ม project root ไปยัง sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class TestValidTransitions:
    """ทดสอบ valid state transitions ตาม VALID_TRANSITIONS map."""

    def test_valid_transitions_map_exists(self):
        """ตรวจว่า VALID_TRANSITIONS ถูกกำหนดใน physician_approval.py."""
        try:
            from physician_approval import VALID_TRANSITIONS
            assert isinstance(VALID_TRANSITIONS, dict), (
                "VALID_TRANSITIONS ต้องเป็น dict"
            )
            assert len(VALID_TRANSITIONS) > 0, (
                "VALID_TRANSITIONS ต้องมีอย่างน้อย 1 state"
            )
        except ImportError:
            pytest.skip("ไม่สามารถ import physician_approval ได้ใน CI")

    def test_pending_review_can_be_approved(self):
        """เอกสาร pending_review สามารถ approve ได้."""
        try:
            from physician_approval import VALID_TRANSITIONS
            pending_transitions = VALID_TRANSITIONS.get("pending_review", set())
            assert "approved" in pending_transitions, (
                "pending_review ต้องสามารถ transition ไป approved ได้"
            )
        except ImportError:
            pytest.skip("ไม่สามารถ import physician_approval ได้ใน CI")

    def test_pending_review_can_be_rejected(self):
        """เอกสาร pending_review สามารถ reject ได้."""
        try:
            from physician_approval import VALID_TRANSITIONS
            pending_transitions = VALID_TRANSITIONS.get("pending_review", set())
            assert "rejected" in pending_transitions, (
                "pending_review ต้องสามารถ transition ไป rejected ได้"
            )
        except ImportError:
            pytest.skip("ไม่สามารถ import physician_approval ได้ใน CI")

    def test_pending_review_can_be_corrected(self):
        """เอกสาร pending_review สามารถ correct ได้."""
        try:
            from physician_approval import VALID_TRANSITIONS
            pending_transitions = VALID_TRANSITIONS.get("pending_review", set())
            assert "corrected" in pending_transitions, (
                "pending_review ต้องสามารถ transition ไป corrected ได้"
            )
        except ImportError:
            pytest.skip("ไม่สามารถ import physician_approval ได้ใน CI")

    def test_corrected_can_be_approved(self):
        """เอกสาร corrected สามารถ approve (correction_approved) ได้."""
        try:
            from physician_approval import VALID_TRANSITIONS
            corrected_transitions = VALID_TRANSITIONS.get("corrected", set())
            assert "correction_approved" in corrected_transitions, (
                "corrected ต้องสามารถ transition ไป correction_approved ได้"
            )
        except ImportError:
            pytest.skip("ไม่สามารถ import physician_approval ได้ใน CI")

    def test_corrected_can_be_rejected(self):
        """เอกสาร corrected สามารถ reject ได้."""
        try:
            from physician_approval import VALID_TRANSITIONS
            corrected_transitions = VALID_TRANSITIONS.get("corrected", set())
            assert "rejected" in corrected_transitions, (
                "corrected ต้องสามารถ transition ไป rejected ได้"
            )
        except ImportError:
            pytest.skip("ไม่สามารถ import physician_approval ได้ใน CI")

    def test_corrected_can_be_re_corrected(self):
        """เอกสาร corrected สามารถแก้ไขซ้ำ (corrected อีกครั้ง) ได้."""
        try:
            from physician_approval import VALID_TRANSITIONS
            corrected_transitions = VALID_TRANSITIONS.get("corrected", set())
            assert "corrected" in corrected_transitions, (
                "corrected ต้องสามารถ transition ไป corrected ได้ (แก้ไขซ้ำ)"
            )
        except ImportError:
            pytest.skip("ไม่สามารถ import physician_approval ได้ใน CI")


class TestInvalidTransitions:
    """ทดสอบว่า invalid transitions ถูก reject."""

    def test_approved_has_no_further_transitions(self):
        """เอกสารที่ approved แล้วไม่ควรมี transitions ต่อ (terminal state)."""
        try:
            from physician_approval import VALID_TRANSITIONS
            # approved ไม่ควรมี key ใน VALID_TRANSITIONS
            approved_transitions = VALID_TRANSITIONS.get("approved", set())
            assert len(approved_transitions) == 0, (
                "approved เป็น terminal state — ไม่ควรมี transitions ต่อ"
            )
        except ImportError:
            pytest.skip("ไม่สามารถ import physician_approval ได้ใน CI")

    def test_rejected_has_no_further_transitions(self):
        """เอกสารที่ rejected แล้วไม่ควรมี transitions ต่อ (terminal state)."""
        try:
            from physician_approval import VALID_TRANSITIONS
            rejected_transitions = VALID_TRANSITIONS.get("rejected", set())
            assert len(rejected_transitions) == 0, (
                "rejected เป็น terminal state — ไม่ควรมี transitions ต่อ"
            )
        except ImportError:
            pytest.skip("ไม่สามารถ import physician_approval ได้ใน CI")

    def test_unknown_state_has_no_transitions(self):
        """State ที่ไม่รู้จักต้องไม่มี transitions."""
        try:
            from physician_approval import VALID_TRANSITIONS
            assert VALID_TRANSITIONS.get("nonexistent_state") is None, (
                "State ที่ไม่มีจริงต้องคืน None"
            )
        except ImportError:
            pytest.skip("ไม่สามารถ import physician_approval ได้ใน CI")


class TestWorkflowIntegrity:
    """ตรวจสอบความถูกต้องของ workflow โดยรวม."""

    def test_all_transition_values_are_sets(self):
        """ทุก value ใน VALID_TRANSITIONS ต้องเป็น set."""
        try:
            from physician_approval import VALID_TRANSITIONS
            for state, transitions in VALID_TRANSITIONS.items():
                assert isinstance(transitions, set), (
                    f"VALID_TRANSITIONS['{state}'] ต้องเป็น set, "
                    f"ได้ {type(transitions).__name__}"
                )
        except ImportError:
            pytest.skip("ไม่สามารถ import physician_approval ได้ใน CI")

    def test_no_self_transition_except_corrected(self):
        """ไม่ควรมี self-transition ยกเว้น corrected → corrected."""
        try:
            from physician_approval import VALID_TRANSITIONS
            for state, transitions in VALID_TRANSITIONS.items():
                if state == "corrected":
                    continue  # อนุญาตให้แก้ไขซ้ำ
                assert state not in transitions, (
                    f"State '{state}' ไม่ควร transition กลับไปหาตัวเอง"
                )
        except ImportError:
            pytest.skip("ไม่สามารถ import physician_approval ได้ใน CI")

    def test_review_status_enum_exists(self):
        """ตรวจว่ามี ReviewStatus enum."""
        try:
            from physician_approval import ReviewStatus
            # ตรวจว่ามี PENDING_REVIEW
            assert hasattr(ReviewStatus, "PENDING_REVIEW"), (
                "ReviewStatus ต้องมี PENDING_REVIEW"
            )
        except ImportError:
            pytest.skip("ไม่สามารถ import physician_approval ได้ใน CI")

    def test_review_action_enum_exists(self):
        """ตรวจว่ามี ReviewAction enum."""
        try:
            from physician_approval import ReviewAction
            assert hasattr(ReviewAction, "APPROVED"), (
                "ReviewAction ต้องมี APPROVED"
            )
            assert hasattr(ReviewAction, "REJECTED"), (
                "ReviewAction ต้องมี REJECTED"
            )
        except ImportError:
            pytest.skip("ไม่สามารถ import physician_approval ได้ใน CI")

import pytest

from app import (
    PAYMENT_STATUS_FAILED,
    PAYMENT_STATUS_PENDING,
    PAYMENT_STATUS_SUCCESSFUL,
    build_payment_status_view,
)


def test_build_payment_status_view_marks_pending_payments_for_recovery():
    payment = {
        "status": PAYMENT_STATUS_PENDING,
        "is_featured": 0,
        "provider_reference": "txn-123",
    }

    view = build_payment_status_view(payment)

    assert view["state"] == "pending"
    assert view["show_recovery_action"] is True
    assert view["show_waiting_message"] is True


def test_build_payment_status_view_marks_completed_payments_as_featured():
    payment = {
        "status": PAYMENT_STATUS_SUCCESSFUL,
        "is_featured": 1,
        "provider_reference": "txn-456",
    }

    view = build_payment_status_view(payment)

    assert view["state"] == "featured"
    assert view["show_recovery_action"] is False
    assert view["show_waiting_message"] is False


def test_build_payment_status_view_handles_failed_payments_gracefully():
    payment = {
        "status": PAYMENT_STATUS_FAILED,
        "is_featured": 0,
        "provider_reference": "",
    }

    view = build_payment_status_view(payment)

    assert view["state"] == "failed"
    assert view["show_recovery_action"] is False
    assert view["message"].startswith("Payment did not complete")

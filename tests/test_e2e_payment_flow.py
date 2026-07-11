"""End-to-end test for the CamerPay payment flow.

Simulates a complete successful payment without spending real money:
  1. Creates a user, listing, and pending payment in the database.
  2. Simulates a CamerPay webhook with a valid HMAC signature and status="completed".
  3. Verifies the webhook handler processes it -> payment becomes SUCCESSFUL,
     listing becomes featured.
  4. Simulates the CamerPay status API returning "completed" and verifies
     that polling and manual recovery also activate the listing.
  5. Verifies the Featured badge appears in the listing detail view.

Run with:  python -m pytest tests/test_e2e_payment_flow.py -v -s
"""

import hashlib
import hmac
import os
import sys
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

# Ensure the project root is on sys.path
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# --- Patch CamerPay API calls BEFORE importing app --------------------------
# We patch get_transaction_status so that polling/recovery/verify see a
# "completed" response without hitting the real API.
MOCK_TRANSACTION_UUID = str(uuid.uuid4())
MOCK_PROVIDER_STATUS = "completed"


def _mock_get_transaction_status(transaction_uuid):
    """Return a fake CamerPay status response that looks like the real API."""
    return {
        "ok": True,
        "status_code": 200,
        "reference": transaction_uuid,
        "pay_url": "",
        "status": MOCK_PROVIDER_STATUS,
        "error": "",
        "data": {
            "uuid": transaction_uuid,
            "status": MOCK_PROVIDER_STATUS,
            "amount": "100.00",
            "currency": "XAF",
            "phone_number": "237671234567",
        },
        "network_error": False,
    }


# Apply the patch at module level so it's in place before any imports
_patcher = patch("services.camerpay.get_transaction_status", _mock_get_transaction_status)
_patcher.start()

# Now safe to import app internals
from app import (
    PAYMENT_STATUS_SUCCESSFUL,
    app,
    apply_successful_payment,
    create_pending_payment_record,
    get_db,
    get_payment_by_id,
    get_payment_local_reference,
    poll_pending_payments_periodic,
    resolve_internal_payment_status,
    update_payment_provider_reference,
)
from services import camerpay as campay


# --- Fixtures -----------------------------------------------------------------


@pytest.fixture(scope="module")
def test_client():
    """Provide a Flask test client."""
    app.config["TESTING"] = True
    app.config["SERVER_NAME"] = "test.local"
    with app.test_client() as client:
        with app.app_context():
            yield client


@pytest.fixture
def db_setup():
    """Create a user, a listing, and a pending payment.

    Yields a dict with keys:
      - user_id, listing_id, payment_id
      - local_reference, provider_reference, phone
    """
    conn = get_db()
    cursor = conn.cursor()

    # 1. Create a unique user
    phone = f"2376{str(uuid.uuid4().int)[:8]}"
    created_at = datetime.utcnow().isoformat(timespec="seconds")
    cursor.execute(
        "INSERT INTO users (full_name, phone, password_hash, auth_provider, created_at) VALUES (?, ?, ?, ?, ?)",
        ("Test User", phone, "fakehash", "local", created_at),
    )
    user_id = cursor.lastrowid

    # 2. Create a listing owned by that user
    leave_date = (datetime.today() + timedelta(days=30)).strftime("%Y-%m-%d")
    cursor.execute(
        "INSERT INTO listings (title, price, category, phone, owner_phone, leave_date, description, image, is_featured) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("Test Listing", "5000", "Other", phone, phone, leave_date, "A test listing", "", 0),
    )
    listing_id = cursor.lastrowid

    # 3. Create a pending payment for that listing
    payment_row = create_pending_payment_record(cursor, listing_id, 100, phone)
    payment_id = payment_row["id"]
    local_reference = get_payment_local_reference(payment_row)

    # 4. Store a provider reference (simulates what initiate_payment would do)
    provider_reference = MOCK_TRANSACTION_UUID
    update_payment_provider_reference(cursor, local_reference, provider_reference)

    conn.commit()

    data = {
        "conn": conn,
        "cursor": cursor,
        "user_id": user_id,
        "listing_id": listing_id,
        "payment_id": payment_id,
        "local_reference": local_reference,
        "provider_reference": provider_reference,
        "phone": phone,
    }
    yield data

    # Teardown: clean up test data
    try:
        cursor.execute("DELETE FROM payments WHERE id = ?", (payment_id,))
        cursor.execute("DELETE FROM listings WHERE id = ?", (listing_id,))
        cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
    finally:
        cursor.close()
        conn.close()


@pytest.fixture
def logged_in_client(test_client, db_setup):
    """Provide a test client with a logged-in session for the created user."""
    data = db_setup
    with test_client.session_transaction() as session:
        session["user_id"] = data["user_id"]
        session["user_name"] = "Test User"
        session["user_phone"] = data["phone"]
    return test_client, data


# --- Helper: build a valid CamerPay webhook payload ---------------------------


def build_webhook_payload(
    *,
    transaction_uuid: str,
    invoice_id: str,
    status: str,
    amount: str = "100.00",
    callback_secret: str = "",
) -> dict:
    """Build a webhook payload dict with a correct HMAC-SHA256 signature.

    The signature is computed over:  uuid|invoice_id|status|amount
    """
    payload = {
        "uuid": transaction_uuid,
        "invoice_id": invoice_id,
        "status": status,
        "amount": amount,
    }
    if callback_secret:
        message = "|".join((transaction_uuid, invoice_id, status, amount))
        sig = hmac.new(
            callback_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        payload["signature"] = f"sha256={sig}"
    return payload


# --- Tests ---------------------------------------------------------------------


class TestWebhookFlow:
    """Test that a real webhook POST with status='completed' activates the listing."""

    def test_webhook_completed_activates_listing(self, db_setup, test_client):
        """Send a webhook with status='completed' and verify the listing is featured."""
        data = db_setup
        callback_secret = os.getenv("CAMERPAY_CALLBACK_SECRET", "")

        # Build a valid webhook payload
        payload = build_webhook_payload(
            transaction_uuid=data["provider_reference"],
            invoice_id=f"listing-{data['listing_id']}-payment-{data['payment_id']}",
            status="completed",
            callback_secret=callback_secret,
        )

        # POST to the webhook endpoint
        resp = test_client.post(
            "/payments/camerpay/webhook",
            json=payload,
            content_type="application/json",
        )
        assert resp.status_code == 200, f"Webhook returned {resp.status_code}: {resp.get_json()}"

        # Verify the payment is now SUCCESSFUL
        conn = get_db()
        cursor = conn.cursor()
        payment = get_payment_by_id(cursor, data["payment_id"])
        conn.close()

        assert payment is not None, "Payment not found after webhook"
        normalized = resolve_internal_payment_status(payment["status"])
        assert normalized == PAYMENT_STATUS_SUCCESSFUL, (
            f"Expected SUCCESSFUL, got {payment['status']} (normalized={normalized})"
        )

        # Verify the listing is featured
        assert payment["is_featured"] == 1, "Listing was not marked as featured"

    def test_webhook_without_signature_still_works_when_secret_missing(self, db_setup, test_client):
        """If CAMERPAY_CALLBACK_SECRET is empty, webhooks without signature are accepted."""
        callback_secret = os.getenv("CAMERPAY_CALLBACK_SECRET", "")
        if callback_secret:
            pytest.skip("CAMERPAY_CALLBACK_SECRET is set — webhooks without signature are rejected")

        data = db_setup
        payload = build_webhook_payload(
            transaction_uuid=data["provider_reference"],
            invoice_id=f"listing-{data['listing_id']}-payment-{data['payment_id']}",
            status="completed",
            callback_secret="",  # no signature
        )

        resp = test_client.post(
            "/payments/camerpay/webhook",
            json=payload,
            content_type="application/json",
        )
        assert resp.status_code == 200, f"Webhook returned {resp.status_code}"

        conn = get_db()
        cursor = conn.cursor()
        payment = get_payment_by_id(cursor, data["payment_id"])
        conn.close()
        normalized = resolve_internal_payment_status(payment["status"])
        assert normalized == PAYMENT_STATUS_SUCCESSFUL

    def test_webhook_invalid_signature_rejected(self, db_setup, test_client):
        """A webhook with a bad signature should be rejected with 403."""
        data = db_setup
        # Only test if a secret is configured
        callback_secret = os.getenv("CAMERPAY_CALLBACK_SECRET", "")
        if not callback_secret:
            pytest.skip("CAMERPAY_CALLBACK_SECRET not set, skipping signature test")

        payload = build_webhook_payload(
            transaction_uuid=data["provider_reference"],
            invoice_id=f"listing-{data['listing_id']}-payment-{data['payment_id']}",
            status="completed",
            callback_secret=callback_secret,
        )
        # Tamper with the signature
        payload["signature"] = "sha256=0000000000000000000000000000000000000000000000000000000000000000"

        resp = test_client.post(
            "/payments/camerpay/webhook",
            json=payload,
            content_type="application/json",
        )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}"

    def test_webhook_completed_maps_via_resolve(self, db_setup, test_client):
        """Verify that 'completed' maps to SUCCESSFUL via resolve_internal_payment_status."""
        normalized = resolve_internal_payment_status("completed")
        assert normalized == PAYMENT_STATUS_SUCCESSFUL, f"'completed' mapped to {normalized}"

        normalized = resolve_internal_payment_status("COMPLETED")
        assert normalized == PAYMENT_STATUS_SUCCESSFUL, f"'COMPLETED' mapped to {normalized}"


class TestPollingFlow:
    """Test that the background poller activates listings when the API returns 'completed'."""

    def test_poller_activates_listing(self, db_setup):
        """Run the poller and verify it activates the pending payment."""
        data = db_setup

        # The poller should find our pending payment and activate it
        poll_pending_payments_periodic()

        conn = get_db()
        cursor = conn.cursor()
        payment = get_payment_by_id(cursor, data["payment_id"])
        conn.close()

        normalized = resolve_internal_payment_status(payment["status"])
        assert normalized == PAYMENT_STATUS_SUCCESSFUL, (
            f"Poller did not activate payment: status={payment['status']} normalized={normalized}"
        )
        assert payment["is_featured"] == 1, "Listing not featured after poller"


class TestRecoveryFlow:
    """Test that manual recovery (POST /payments/recover/<id>) activates the listing."""

    def test_recovery_activates_listing(self, logged_in_client):
        """POST to the recovery endpoint and verify activation."""
        test_client, data = logged_in_client

        resp = test_client.post(
            f"/payments/recover/{data['local_reference']}",
            data={"next": f"/listing/{data['listing_id']}"},
        )
        # Recovery redirects to verify page
        assert resp.status_code in (302,), f"Recovery returned {resp.status_code}"

        conn = get_db()
        cursor = conn.cursor()
        payment = get_payment_by_id(cursor, data["payment_id"])
        conn.close()

        normalized = resolve_internal_payment_status(payment["status"])
        assert normalized == PAYMENT_STATUS_SUCCESSFUL, (
            f"Recovery did not activate payment: status={payment['status']}"
        )
        assert payment["is_featured"] == 1, "Listing not featured after recovery"


class TestVerifyFlow:
    """Test that the verify page shows the Featured badge after activation."""

    def test_verify_page_shows_featured(self, logged_in_client):
        """GET the verify page and check the rendered HTML for the Featured badge."""
        test_client, data = logged_in_client

        # First, activate via apply_successful_payment directly (simulates webhook)
        conn = get_db()
        cursor = conn.cursor()
        payment = get_payment_by_id(cursor, data["payment_id"])
        apply_successful_payment(cursor, payment)
        conn.commit()
        conn.close()

        # Now visit the verify page
        resp = test_client.get(
            f"/payments/verify/{data['local_reference']}",
            follow_redirects=True,
        )
        assert resp.status_code == 200, f"Verify page returned {resp.status_code}"

        html = resp.data.decode("utf-8").lower()
        # Check for the Featured badge text
        assert "featured" in html or "payment confirmed" in html, (
            "Verify page does not show Featured/confirmed status"
        )

    def test_listing_detail_shows_featured_badge(self, db_setup, test_client):
        """View the listing detail page and check for the Featured badge."""
        data = db_setup

        # Activate the listing
        conn = get_db()
        cursor = conn.cursor()
        payment = get_payment_by_id(cursor, data["payment_id"])
        apply_successful_payment(cursor, payment)
        conn.commit()
        conn.close()

        # Visit the listing detail page
        resp = test_client.get(
            f"/listing/{data['listing_id']}",
            follow_redirects=True,
        )
        assert resp.status_code == 200, f"Listing detail returned {resp.status_code}"

        html = resp.data.decode("utf-8").lower()
        # The listing should show some indication of being featured
        assert "featured" in html, "Listing detail page does not mention 'featured'"


class TestStatusAPIFlow:
    """Test that the CamerPay status API returning 'completed' works end-to-end."""

    def test_get_transaction_status_returns_completed(self):
        """Verify our mock returns the expected status."""
        result = campay.get_transaction_status(MOCK_TRANSACTION_UUID)
        assert result.get("status") == "completed", f"Mock returned status={result.get('status')}"
        assert result.get("ok") is True
        assert result.get("network_error") is False

    def test_resolve_handles_api_status(self):
        """Verify that the status string from the API maps to SUCCESSFUL."""
        result = campay.get_transaction_status(MOCK_TRANSACTION_UUID)
        normalized = resolve_internal_payment_status(result.get("status"))
        assert normalized == PAYMENT_STATUS_SUCCESSFUL, (
            f"API status '{result.get('status')}' mapped to {normalized}"
        )


class TestIdempotency:
    """Test that already-successful payments are not double-activated."""

    def test_webhook_idempotent(self, db_setup, test_client):
        """Sending the same webhook twice should not cause errors."""
        data = db_setup
        callback_secret = os.getenv("CAMERPAY_CALLBACK_SECRET", "")

        payload = build_webhook_payload(
            transaction_uuid=data["provider_reference"],
            invoice_id=f"listing-{data['listing_id']}-payment-{data['payment_id']}",
            status="completed",
            callback_secret=callback_secret,
        )

        # First webhook
        resp1 = test_client.post("/payments/camerpay/webhook", json=payload)
        assert resp1.status_code == 200

        # Second webhook (same payload)
        resp2 = test_client.post("/payments/camerpay/webhook", json=payload)
        assert resp2.status_code == 200

        # Payment should still be SUCCESSFUL
        conn = get_db()
        cursor = conn.cursor()
        payment = get_payment_by_id(cursor, data["payment_id"])
        conn.close()
        normalized = resolve_internal_payment_status(payment["status"])
        assert normalized == PAYMENT_STATUS_SUCCESSFUL
        assert payment["is_featured"] == 1

    def test_recovery_idempotent(self, db_setup, test_client):
        """Calling recovery on an already-successful payment should be safe."""
        data = db_setup

        # Activate first
        conn = get_db()
        cursor = conn.cursor()
        payment = get_payment_by_id(cursor, data["payment_id"])
        apply_successful_payment(cursor, payment)
        conn.commit()
        conn.close()

        # Recovery again
        resp = test_client.post(
            f"/payments/recover/{data['local_reference']}",
            data={"next": f"/listing/{data['listing_id']}"},
        )
        assert resp.status_code in (302,)

        # Still successful
        conn = get_db()
        cursor = conn.cursor()
        payment = get_payment_by_id(cursor, data["payment_id"])
        conn.close()
        assert payment["is_featured"] == 1


class TestVerifyErrorHandling:
    """Test that API errors don't corrupt a successful payment."""

    def test_verify_does_not_downgrade_successful(self, db_setup, test_client):
        """If a payment is already SUCCESSFUL, verify should not change it."""
        data = db_setup

        # Activate first
        conn = get_db()
        cursor = conn.cursor()
        payment = get_payment_by_id(cursor, data["payment_id"])
        apply_successful_payment(cursor, payment)
        conn.commit()
        conn.close()

        # Visit verify page
        resp = test_client.get(
            f"/payments/verify/{data['local_reference']}",
            follow_redirects=True,
        )
        assert resp.status_code == 200

        # Still successful
        conn = get_db()
        cursor = conn.cursor()
        payment = get_payment_by_id(cursor, data["payment_id"])
        conn.close()
        normalized = resolve_internal_payment_status(payment["status"])
        assert normalized == PAYMENT_STATUS_SUCCESSFUL
        assert payment["is_featured"] == 1


# --- Cleanup the module-level patcher ------------------------------------------


def teardown_module(module):
    _patcher.stop()
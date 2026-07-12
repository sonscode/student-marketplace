import io
import uuid
from datetime import datetime, timedelta, UTC

import pytest

from app import app, get_db


def _unique_phone():
    return f"2376{str(uuid.uuid4().int)[:8]}"


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture
def ux_user():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(listings)")
    listing_columns = {row["name"] for row in cursor.fetchall()}
    if "updated_at" not in listing_columns:
        cursor.execute("ALTER TABLE listings ADD COLUMN updated_at TEXT")

    cursor.execute("PRAGMA table_info(users)")
    user_columns = {row["name"] for row in cursor.fetchall()}
    if "is_verified" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN is_verified INTEGER DEFAULT 0")

    phone = _unique_phone()
    created_at = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")
    cursor.execute(
        "INSERT INTO users (full_name, phone, password_hash, auth_provider, created_at) VALUES (?, ?, ?, ?, ?)",
        ("UX Test User", phone, "fakehash", "local", created_at),
    )
    user_id = cursor.lastrowid
    conn.commit()

    yield {"id": user_id, "phone": phone, "created_at": created_at}

    cursor.execute(
        "DELETE FROM payments WHERE listing_id IN (SELECT id FROM listings WHERE owner_phone = ?)",
        (phone,),
    )
    cursor.execute("DELETE FROM listings WHERE owner_phone = ?", (phone,))
    cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()


def _create_listing(cursor, *, phone, title, leave_date, is_featured=0, updated_at=None):
    if updated_at:
        cursor.execute(
            """
            INSERT INTO listings (title, price, category, phone, owner_phone, leave_date, description, image, is_featured, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (title, "1500", "Other", phone, phone, leave_date, "Regression listing", "", is_featured, updated_at),
        )
    else:
        cursor.execute(
            """
            INSERT INTO listings (title, price, category, phone, owner_phone, leave_date, description, image, is_featured)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (title, "1500", "Other", phone, phone, leave_date, "Regression listing", "", is_featured),
        )
    return cursor.lastrowid


def test_public_home_keeps_recent_expired_listings_visible(client, ux_user):
    conn = get_db()
    cursor = conn.cursor()
    active_title = f"Active UX {uuid.uuid4()}"
    recent_expired_title = f"Recent Expired UX {uuid.uuid4()}"
    old_expired_title = f"Old Expired UX {uuid.uuid4()}"
    active_date = (datetime.today() + timedelta(days=5)).strftime("%Y-%m-%d")
    recent_expired_date = (datetime.today() - timedelta(days=5)).strftime("%Y-%m-%d")
    old_expired_date = (datetime.today() - timedelta(days=75)).strftime("%Y-%m-%d")
    _create_listing(cursor, phone=ux_user["phone"], title=active_title, leave_date=active_date)
    _create_listing(cursor, phone=ux_user["phone"], title=recent_expired_title, leave_date=recent_expired_date)
    _create_listing(cursor, phone=ux_user["phone"], title=old_expired_title, leave_date=old_expired_date)
    conn.commit()
    conn.close()

    response = client.get("/")
    html = response.data.decode()

    assert response.status_code == 200
    assert active_title in html
    assert recent_expired_title in html
    assert old_expired_title not in html
    assert "Expired Listings" in html


def test_featured_filter_only_returns_featured_visible_listings(client, ux_user):
    conn = get_db()
    cursor = conn.cursor()
    featured_title = f"Featured UX {uuid.uuid4()}"
    active_title = f"Active Nonfeatured UX {uuid.uuid4()}"
    expired_title = f"Expired Nonfeatured UX {uuid.uuid4()}"
    active_date = (datetime.today() + timedelta(days=5)).strftime("%Y-%m-%d")
    expired_date = (datetime.today() - timedelta(days=5)).strftime("%Y-%m-%d")
    _create_listing(cursor, phone=ux_user["phone"], title=featured_title, leave_date=active_date, is_featured=1)
    _create_listing(cursor, phone=ux_user["phone"], title=active_title, leave_date=active_date, is_featured=0)
    _create_listing(cursor, phone=ux_user["phone"], title=expired_title, leave_date=expired_date, is_featured=0)
    conn.commit()
    conn.close()

    response = client.get("/?show=featured")
    html = response.data.decode()

    assert response.status_code == 200
    assert featured_title in html
    assert active_title not in html
    assert expired_title not in html


def test_active_filter_returns_active_listings_even_when_featured(client, ux_user):
    conn = get_db()
    cursor = conn.cursor()
    featured_title = f"Active Featured UX {uuid.uuid4()}"
    active_title = f"Active Plain UX {uuid.uuid4()}"
    expired_title = f"Expired UX {uuid.uuid4()}"
    active_date = (datetime.today() + timedelta(days=5)).strftime("%Y-%m-%d")
    expired_date = (datetime.today() - timedelta(days=5)).strftime("%Y-%m-%d")
    _create_listing(cursor, phone=ux_user["phone"], title=featured_title, leave_date=active_date, is_featured=1)
    _create_listing(cursor, phone=ux_user["phone"], title=active_title, leave_date=active_date, is_featured=0)
    _create_listing(cursor, phone=ux_user["phone"], title=expired_title, leave_date=expired_date, is_featured=1)
    conn.commit()
    conn.close()

    response = client.get("/?show=active")
    html = response.data.decode()

    assert response.status_code == 200
    assert featured_title in html
    assert active_title in html
    assert expired_title not in html


def test_expired_listing_detail_disables_contact(client, ux_user):
    conn = get_db()
    cursor = conn.cursor()
    expired_date = (datetime.today() - timedelta(days=2)).strftime("%Y-%m-%d")
    listing_id = _create_listing(
        cursor,
        phone=ux_user["phone"],
        title=f"Expired Detail UX {uuid.uuid4()}",
        leave_date=expired_date,
    )
    conn.commit()
    conn.close()

    response = client.get(f"/listing/{listing_id}")
    html = response.data.decode()

    assert response.status_code == 200
    assert "This listing has expired." in html
    assert "Contact unavailable - listing expired" in html
    assert "https://wa.me/" not in html


def test_active_listing_detail_uses_whatsapp_contact_button(client, ux_user):
    conn = get_db()
    cursor = conn.cursor()
    active_date = (datetime.today() + timedelta(days=2)).strftime("%Y-%m-%d")
    listing_id = _create_listing(
        cursor,
        phone=ux_user["phone"],
        title=f"Active Detail UX {uuid.uuid4()}",
        leave_date=active_date,
    )
    conn.commit()
    conn.close()

    response = client.get(f"/listing/{listing_id}")
    html = response.data.decode()

    assert response.status_code == 200
    assert 'class="cta whatsapp"' in html
    assert "https://wa.me/" in html
    assert "whatsapp-icon" in html


def test_admin_can_toggle_seller_verification_and_badges_display(client, ux_user):
    conn = get_db()
    cursor = conn.cursor()
    admin_phone = _unique_phone()
    created_at = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")
    cursor.execute(
        """
        INSERT INTO users (full_name, phone, password_hash, auth_provider, is_admin, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("Verification Admin", admin_phone, "fakehash", "local", 1, created_at),
    )
    admin_id = cursor.lastrowid
    active_date = (datetime.today() + timedelta(days=3)).strftime("%Y-%m-%d")
    listing_title = f"Verified Seller UX {uuid.uuid4()}"
    listing_id = _create_listing(
        cursor,
        phone=ux_user["phone"],
        title=listing_title,
        leave_date=active_date,
    )
    conn.commit()
    conn.close()

    try:
        with client.session_transaction() as session:
            session["user_id"] = admin_id
            session["user_name"] = "Verification Admin"
            session["user_phone"] = admin_phone

        response = client.post(f"/admin/users/{ux_user['id']}/toggle-verified")
        assert response.status_code == 302

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT is_verified FROM users WHERE id = ?", (ux_user["id"],))
        seller = cursor.fetchone()
        conn.close()
        assert seller["is_verified"] == 1

        detail_response = client.get(f"/listing/{listing_id}")
        detail_html = detail_response.data.decode()
        assert detail_response.status_code == 200
        assert "Verified" in detail_html

        home_response = client.get("/")
        home_html = home_response.data.decode()
        assert home_response.status_code == 200
        assert listing_title in home_html
        assert "Verified Seller" in home_html
    finally:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE id = ?", (admin_id,))
        conn.commit()
        conn.close()


def test_non_admin_cannot_toggle_seller_verification(client, ux_user):
    with client.session_transaction() as session:
        session["user_id"] = ux_user["id"]
        session["user_name"] = "UX Test User"
        session["user_phone"] = ux_user["phone"]

    response = client.post(f"/admin/users/{ux_user['id']}/toggle-verified")

    assert response.status_code == 403

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT is_verified FROM users WHERE id = ?", (ux_user["id"],))
    seller = cursor.fetchone()
    conn.close()
    assert seller["is_verified"] == 0


def test_admin_user_action_forms_are_not_nested_in_bulk_delete_form(client, ux_user):
    conn = get_db()
    cursor = conn.cursor()
    admin_phone = _unique_phone()
    created_at = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")
    cursor.execute(
        """
        INSERT INTO users (full_name, phone, password_hash, auth_provider, is_admin, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("Form Admin", admin_phone, "fakehash", "local", 1, created_at),
    )
    admin_id = cursor.lastrowid
    conn.commit()
    conn.close()

    try:
        with client.session_transaction() as session:
            session["user_id"] = admin_id
            session["user_name"] = "Form Admin"
            session["user_phone"] = admin_phone

        response = client.get("/admin")
        html = response.data.decode()

        assert response.status_code == 200
        bulk_form_start = html.index('id="bulk-delete-users-form"')
        bulk_form_end = html.index("</form>", bulk_form_start)
        users_table_start = html.index("<table>", bulk_form_end)
        verify_form_start = html.index("/toggle-verified")

        assert bulk_form_end < users_table_start
        assert bulk_form_start < verify_form_start
        assert not (bulk_form_start < verify_form_start < bulk_form_end)
        assert 'form="bulk-delete-users-form" type="checkbox" name="user_ids"' in html
    finally:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE id = ?", (admin_id,))
        conn.commit()
        conn.close()


def test_listing_detail_shows_seller_trust_information(client, ux_user):
    conn = get_db()
    cursor = conn.cursor()
    active_date = (datetime.today() + timedelta(days=5)).strftime("%Y-%m-%d")
    updated_at = "2026-01-02T03:04:00"
    listing_id = _create_listing(
        cursor,
        phone=ux_user["phone"],
        title=f"Trust Detail UX {uuid.uuid4()}",
        leave_date=active_date,
        updated_at=updated_at,
    )
    _create_listing(
        cursor,
        phone=ux_user["phone"],
        title=f"Trust Count UX {uuid.uuid4()}",
        leave_date=active_date,
    )
    conn.commit()
    conn.close()

    response = client.get(f"/listing/{listing_id}")
    html = response.data.decode()
    expected_member_since = datetime.fromisoformat(ux_user["created_at"]).strftime("%b %d, %Y").replace(" 0", " ")

    assert response.status_code == 200
    assert "Member Since" in html
    assert expected_member_since in html
    assert "Seller Active Listings" in html
    assert ">2</strong>" in html
    assert "Last Updated" in html
    assert "Jan 2, 2026 at 3:04" in html


def test_create_listing_rejects_past_leave_date_before_upload(client, ux_user):
    with client.session_transaction() as session:
        session["user_id"] = ux_user["id"]
        session["user_name"] = "UX Test User"
        session["user_phone"] = ux_user["phone"]

    response = client.post(
        "/add",
        data={
            "title": "Past Date UX",
            "price": "1500",
            "phone": ux_user["phone"],
            "leave_date": "2020-01-01",
            "category": "Other",
            "description": "Past date should be rejected",
            "image": (io.BytesIO(b"fake image"), "listing.png"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert b"Leaving date cannot be in the past." in response.data


def test_styled_error_pages_replace_bare_403_and_404(client, ux_user):
    with client.session_transaction() as session:
        session["user_id"] = ux_user["id"]
        session["user_name"] = "UX Test User"
        session["user_phone"] = ux_user["phone"]

    forbidden_response = client.get("/admin")
    not_found_response = client.get("/listing/999999999")

    assert forbidden_response.status_code == 403
    assert b"Admin access required" in forbidden_response.data
    assert b"Go to Listings" in forbidden_response.data

    assert not_found_response.status_code == 404
    assert b"Listing not found" in not_found_response.data
    assert b"Go to Listings" in not_found_response.data


def test_dashboard_displays_featured_and_payment_status_icons(client, ux_user):
    conn = get_db()
    cursor = conn.cursor()
    active_date = (datetime.today() + timedelta(days=5)).strftime("%Y-%m-%d")
    listing_id = _create_listing(
        cursor,
        phone=ux_user["phone"],
        title=f"Dashboard Icons UX {uuid.uuid4()}",
        leave_date=active_date,
        is_featured=1,
    )
    created_at = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")
    statuses = ("SUCCESSFUL", "PENDING", "FAILED", "CANCELLED", "REJECTED", "EXPIRED")
    for status in statuses:
        cursor.execute(
            """
            INSERT INTO payments (listing_id, reference, amount, status, phone, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (listing_id, f"ux-icons-{uuid.uuid4()}", 100, status, ux_user["phone"], created_at),
        )
    conn.commit()
    conn.close()

    with client.session_transaction() as session:
        session["user_id"] = ux_user["id"]
        session["user_name"] = "UX Test User"
        session["user_phone"] = ux_user["phone"]

    response = client.get("/dashboard")
    html = response.data.decode()

    assert response.status_code == 200
    assert '🔥 Featured' in html
    assert 'class="chip featured"' in html
    for icon, label in (
        ("✅", "Completed"),
        ("⏳", "Pending"),
        ("❌", "Failed"),
        ("↩", "Cancelled"),
        ("🚫", "Rejected"),
        ("⏰", "Expired"),
    ):
        assert f'<span class="payment-status-icon" aria-hidden="true">{icon}</span>{label}' in html

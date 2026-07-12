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
    phone = _unique_phone()
    created_at = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")
    cursor.execute(
        "INSERT INTO users (full_name, phone, password_hash, auth_provider, created_at) VALUES (?, ?, ?, ?, ?)",
        ("UX Test User", phone, "fakehash", "local", created_at),
    )
    user_id = cursor.lastrowid
    conn.commit()

    yield {"id": user_id, "phone": phone}

    cursor.execute("DELETE FROM listings WHERE owner_phone = ?", (phone,))
    cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()


def _create_listing(cursor, *, phone, title, leave_date):
    cursor.execute(
        """
        INSERT INTO listings (title, price, category, phone, owner_phone, leave_date, description, image, is_featured)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (title, "1500", "Other", phone, phone, leave_date, "Regression listing", "", 0),
    )
    return cursor.lastrowid


def test_public_home_hides_expired_listings_by_default(client, ux_user):
    conn = get_db()
    cursor = conn.cursor()
    active_title = f"Active UX {uuid.uuid4()}"
    expired_title = f"Expired UX {uuid.uuid4()}"
    active_date = (datetime.today() + timedelta(days=5)).strftime("%Y-%m-%d")
    expired_date = (datetime.today() - timedelta(days=5)).strftime("%Y-%m-%d")
    _create_listing(cursor, phone=ux_user["phone"], title=active_title, leave_date=active_date)
    _create_listing(cursor, phone=ux_user["phone"], title=expired_title, leave_date=expired_date)
    conn.commit()
    conn.close()

    response = client.get("/")
    html = response.data.decode()

    assert response.status_code == 200
    assert active_title in html
    assert expired_title not in html
    assert "Expired Listings" not in html


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

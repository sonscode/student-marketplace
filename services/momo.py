import base64
import os
from typing import Any

import requests


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _base_url() -> str:
    return _env("MOMO_BASE_URL", "https://sandbox.momodeveloper.mtn.com").rstrip("/")


def _target_environment() -> str:
    return _env("MOMO_TARGET_ENV", "sandbox")


def _currency() -> str:
    return _env("MOMO_CURRENCY", "EUR")


def _timeout_seconds() -> int:
    raw_timeout = _env("MOMO_HTTP_TIMEOUT_SECONDS", "20")
    try:
        value = int(raw_timeout)
        return value if value > 0 else 20
    except ValueError:
        return 20


def _subscription_key() -> str:
    return _env("MOMO_SUBSCRIPTION_KEY")


def _safe_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text}


def _build_basic_auth_header() -> str:
    api_user = _env("MOMO_API_USER")
    api_key = _env("MOMO_API_KEY")
    if not api_user or not api_key:
        return ""

    encoded = base64.b64encode(f"{api_user}:{api_key}".encode("utf-8")).decode("utf-8")
    return f"Basic {encoded}"


def get_access_token() -> dict[str, Any]:
    auth_header = _build_basic_auth_header()
    subscription_key = _subscription_key()
    if not auth_header or not subscription_key:
        return {
            "ok": False,
            "error": "Missing MTN MoMo credentials. Check MOMO_API_USER, MOMO_API_KEY, and MOMO_SUBSCRIPTION_KEY.",
            "status_code": 0,
        }

    headers = {
        "Authorization": auth_header,
        "Ocp-Apim-Subscription-Key": subscription_key,
    }

    try:
        response = requests.post(
            f"{_base_url()}/collection/token/",
            headers=headers,
            timeout=_timeout_seconds(),
        )
    except requests.RequestException as exc:
        return {
            "ok": False,
            "error": f"Unable to request MoMo token: {exc}",
            "status_code": 0,
        }

    payload = _safe_json(response)
    access_token = payload.get("access_token") if isinstance(payload, dict) else None

    if response.status_code != 200 or not access_token:
        return {
            "ok": False,
            "error": "MoMo token request failed.",
            "status_code": response.status_code,
            "data": payload,
        }

    return {
        "ok": True,
        "access_token": access_token,
        "status_code": response.status_code,
        "data": payload,
    }


def request_to_pay(
    *,
    reference_id: str,
    phone: str,
    amount: int,
    external_id: str,
    payer_message: str = "Boost Listing",
    payee_note: str = "Azison Marketplace",
    callback_url: str | None = None,
) -> dict[str, Any]:
    token_result = get_access_token()
    if not token_result.get("ok"):
        return {
            "ok": False,
            "reference_id": reference_id,
            "status_code": token_result.get("status_code", 0),
            "error": token_result.get("error", "Failed to get MoMo token."),
            "data": token_result.get("data", {}),
        }

    headers = {
        "Authorization": f"Bearer {token_result['access_token']}",
        "X-Reference-Id": reference_id,
        "X-Target-Environment": _target_environment(),
        "Ocp-Apim-Subscription-Key": _subscription_key(),
        "Content-Type": "application/json",
    }
    if callback_url:
        headers["X-Callback-Url"] = callback_url

    payload = {
        "amount": str(amount),
        "currency": _currency(),
        "externalId": external_id,
        "payer": {
            "partyIdType": "MSISDN",
            "partyId": phone,
        },
        "payerMessage": payer_message,
        "payeeNote": payee_note,
    }

    try:
        response = requests.post(
            f"{_base_url()}/collection/v1_0/requesttopay",
            json=payload,
            headers=headers,
            timeout=_timeout_seconds(),
        )
    except requests.RequestException as exc:
        return {
            "ok": False,
            "reference_id": reference_id,
            "status_code": 0,
            "error": f"Unable to submit payment request: {exc}",
            "data": {},
        }

    data = _safe_json(response)
    return {
        "ok": response.status_code == 202,
        "reference_id": reference_id,
        "status_code": response.status_code,
        "data": data,
        "error": "" if response.status_code == 202 else "MoMo request-to-pay was not accepted.",
    }


def get_request_to_pay_status(reference_id: str) -> dict[str, Any]:
    token_result = get_access_token()
    if not token_result.get("ok"):
        return {
            "ok": False,
            "reference_id": reference_id,
            "status_code": token_result.get("status_code", 0),
            "status": "",
            "error": token_result.get("error", "Failed to get MoMo token."),
            "data": token_result.get("data", {}),
        }

    headers = {
        "Authorization": f"Bearer {token_result['access_token']}",
        "X-Target-Environment": _target_environment(),
        "Ocp-Apim-Subscription-Key": _subscription_key(),
    }

    try:
        response = requests.get(
            f"{_base_url()}/collection/v1_0/requesttopay/{reference_id}",
            headers=headers,
            timeout=_timeout_seconds(),
        )
    except requests.RequestException as exc:
        return {
            "ok": False,
            "reference_id": reference_id,
            "status_code": 0,
            "status": "",
            "error": f"Unable to fetch payment status: {exc}",
            "data": {},
        }

    data = _safe_json(response)
    status = ""
    if isinstance(data, dict):
        status = str(data.get("status") or "").upper().strip()

    return {
        "ok": response.ok,
        "reference_id": reference_id,
        "status_code": response.status_code,
        "status": status,
        "data": data,
        "error": "" if response.ok else "MoMo status check failed.",
    }

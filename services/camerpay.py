import hashlib
import hmac
import json
import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://camerpay.biz/api"
HTTP_TIMEOUT_SECONDS = 20


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _api_token() -> str:
    """Return CAMERPAY_API_TOKEN, with fallback to CAMERPAY_TOKEN."""
    token = _env("CAMERPAY_API_TOKEN")
    if not token:
        token = _env("CAMERPAY_TOKEN")
    return token


def _base_url() -> str:
    return _env("CAMERPAY_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _api_url(path: str) -> str:
    normalized_path = (path or "").lstrip("/")
    return f"{_base_url()}/{normalized_path}"


def _safe_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text}


def _auth_headers() -> dict[str, str]:
    token = _api_token()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _missing_token_result() -> dict[str, Any]:
    return {
        "ok": False,
        "status_code": 0,
        "reference": "",
        "pay_url": "",
        "status": "",
        "error": "Missing CamerPay API token. Set CAMERPAY_API_TOKEN or CAMERPAY_TOKEN.",
        "data": {},
        "network_error": False,
    }


def _return_url() -> str:
    """Derive merchant_return_url from CAMERPAY_RETURN_URL or from callback URL."""
    return _env("CAMERPAY_RETURN_URL", "")


def initiate_payment(
    *,
    amount: int,
    invoice_id: str,
    callback_url: str,
    description: str = "Boost Listing",
    customer_phone: str = "",
    customer_id: str = "",
) -> dict[str, Any]:
    """Initiate a payment via the CamerPay API.

    POST /payment/initiate
    Payload fields per CamerPay docs:
      merchant_invoice_id  (string, required)
      amount               (number, required) — in XAF
      currency             (string, default "XAF")
      description          (string)
      merchant_callback_url (string)
      merchant_return_url   (string)
      source               (string, default "api")
      customer_phone       (string)
      customer_id          (string)

    Returns transaction_uuid and pay_url from CamerPay.
    """
    if not _api_token():
        return _missing_token_result()

    callback_url = str(callback_url)
    request_url = _api_url("/payment/initiate")
    headers = _auth_headers()
    token_preview = (headers.get("Authorization", "")[:30] + "...") if headers.get("Authorization") else "MISSING"

    # Build payload per CamerPay docs
    payload: dict[str, Any] = {
        "merchant_invoice_id": str(invoice_id),
        "amount": int(amount),
        "currency": "XAF",
        "description": str(description),
        "merchant_callback_url": callback_url,
        "merchant_return_url": _return_url() or callback_url,
        "source": "api",
    }
    if customer_phone:
        payload["customer_phone"] = str(customer_phone)
    if customer_id:
        payload["customer_id"] = str(customer_id)

    logger.error(
        "camerpay_initiate_request details=%s",
        json.dumps({
            "invoice_id": invoice_id,
            "url": request_url,
            "amount": amount,
            "phone_masked": f"***{str(customer_phone)[-4:]}" if customer_phone else "",
            "has_callback_url": bool(callback_url),
            "auth_token_preview": token_preview,
            "payload_keys": sorted(payload.keys()),
            "payload_preview": {k: (v if k not in ("customer_phone",) else f"***{str(v)[-4:]}") for k, v in payload.items()},
        }, default=str),
    )

    response = None
    try:
        response = requests.post(
            request_url,
            json=payload,
            headers=headers,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        logger.error(
            "camerpay_initiate_network_error details=%s",
            json.dumps({
                "invoice_id": invoice_id,
                "url": request_url,
                "error": str(exc),
                "error_type": type(exc).__name__,
            }, default=str),
        )
        return {
            "ok": False,
            "status_code": 0,
            "reference": "",
            "pay_url": "",
            "status": "",
            "error": f"Unable to initiate CamerPay payment (network/DNS error): {exc}",
            "data": {},
            "network_error": True,
        }

    data = _safe_json(response)
    transaction_uuid = ""
    pay_url = ""
    status = ""
    success = False
    if isinstance(data, dict):
        transaction_uuid = str(data.get("transaction_uuid") or data.get("uuid") or "").strip()
        pay_url = str(data.get("pay_url") or "").strip()
        status = str(data.get("status") or "").strip()
        success = data.get("success") is True

    ok = bool(response.ok and success and transaction_uuid and pay_url)
    error = ""
    if not ok:
        if isinstance(data, dict):
            error = str(
                data.get("message")
                or data.get("reason")
                or data.get("detail")
                or data.get("error")
                or json.dumps(data, default=str)
                or "CamerPay initiation failed."
            ).strip()
        else:
            error = f"CamerPay initiation failed. Raw response: {response.text[:1000]}"

    logger.error(
        "camerpay_initiate_response details=%s",
        json.dumps({
            "invoice_id": invoice_id,
            "url": request_url,
            "http_status": response.status_code,
            "ok": ok,
            "transaction_uuid": transaction_uuid,
            "pay_url_provided": bool(pay_url),
            "provider_status": status,
            "error": error,
            "response_headers": dict(response.headers),
            "response_body_full": str(data)[:2000] if data else response.text[:2000],
        }, default=str),
    )

    return {
        "ok": ok,
        "status_code": response.status_code,
        "reference": transaction_uuid,
        "pay_url": pay_url,
        "status": status,
        "error": error,
        "data": data,
        "network_error": False,
    }


def request_collect(
    *,
    phone: str,
    amount: int,
    external_reference: str,
    external_user: str = "",
    description: str = "Boost Listing",
) -> dict[str, Any]:
    """Initiate a collect (payment request) via CamerPay.

    Wraps initiate_payment with the expected interface used by app.py.
    The caller should redirect the user to pay_url on success.
    """
    result = initiate_payment(
        amount=amount,
        invoice_id=external_reference,
        callback_url=_env("CAMERPAY_CALLBACK_URL", ""),
        description=description,
        customer_phone=phone,
        customer_id=external_user,
    )

    # Preserve the ussd_code from raw data if present (for backward compat)
    ussd_code = ""
    if isinstance(result.get("data"), dict) and not result.get("pay_url"):
        ussd_code = str(result["data"].get("ussd_code") or "").strip()

    return {
        "ok": result.get("ok", False),
        "status_code": result.get("status_code", 0),
        "reference": result.get("reference", ""),
        "pay_url": result.get("pay_url", ""),
        "status": result.get("status", ""),
        "error": result.get("error", ""),
        "data": result.get("data", {}),
        "network_error": result.get("network_error", False),
        "ussd_code": ussd_code,
    }


def get_transaction_status(transaction_uuid: str) -> dict[str, Any]:
    """Check the status of a CamerPay transaction.

    GET /payment/status/{transaction_uuid}
    """
    if not _api_token():
        return _missing_token_result()

    if not transaction_uuid:
        return {
            "ok": False,
            "status_code": 0,
            "reference": "",
            "pay_url": "",
            "status": "",
            "error": "Missing transaction UUID.",
            "data": {},
            "network_error": False,
        }

    logger.error("camerpay_status_request transaction_uuid=%s", transaction_uuid)

    try:
        response = requests.get(
            _api_url(f"/payment/status/{transaction_uuid}"),
            headers=_auth_headers(),
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        logger.error("camerpay_status_network_error transaction_uuid=%s error=%s", transaction_uuid, exc)
        return {
            "ok": False,
            "status_code": 0,
            "reference": transaction_uuid,
            "pay_url": "",
            "status": "",
            "error": f"Unable to check CamerPay status (network/DNS error): {exc}",
            "data": {},
            "network_error": True,
        }

    data = _safe_json(response)
    status = ""
    success = False
    if isinstance(data, dict):
        status = str(data.get("status") or "").strip()
        success = data.get("success") is True

    ok = bool(response.ok and success)

    logger.error(
        "camerpay_status_response transaction_uuid=%s http_status=%s ok=%s provider_status=%s response=%s",
        transaction_uuid,
        response.status_code,
        ok,
        status,
        json.dumps(str(data)[:1000], default=str),
    )

    return {
        "ok": ok,
        "status_code": response.status_code,
        "reference": transaction_uuid,
        "pay_url": "",
        "status": status,
        "error": "" if ok else str(data.get("message") or "Status check failed.") if isinstance(data, dict) else "Status check failed.",
        "data": data,
        "network_error": False,
    }


def verify_webhook_signature(payload: dict[str, Any], callback_secret: str) -> dict[str, Any]:
    """Verify the HMAC-SHA256 signature on a CamerPay webhook payload.

    The message format is: uuid|invoice_id|status|amount
    The 'uuid' field in the payload is the transaction identifier.
    The signature is in the 'signature' field, prefixed with 'sha256='.

    Supports both 'uuid' and 'transaction_uuid' field names.
    """
    signature = str(payload.get("signature") or "").strip()
    if not callback_secret:
        return {"ok": False, "error": "CAMERPAY_CALLBACK_SECRET is not configured."}
    if not signature:
        return {"ok": False, "error": "Signature missing in webhook payload."}

    # Determine the transaction UUID field (CamerPay sends 'uuid', but we support 'transaction_uuid' too)
    transaction_uuid = str(payload.get("uuid") or payload.get("transaction_uuid") or "").strip()
    invoice_id = str(payload.get("invoice_id") or "").strip()
    status = str(payload.get("status") or "").strip()
    # Amount may come as a decimal string like "10000.00" — strip the decimal for signature
    raw_amount = str(payload.get("amount") or "").strip()
    amount = raw_amount.split(".")[0] if "." in raw_amount else raw_amount

    message = "|".join((transaction_uuid, invoice_id, status, amount))
    expected = hmac.new(
        callback_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    provided = signature.removeprefix("sha256=").strip()

    if not hmac.compare_digest(expected, provided):
        return {"ok": False, "error": "Invalid CamerPay webhook signature."}

    return {"ok": True, "error": ""}
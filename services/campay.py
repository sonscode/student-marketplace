import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)

try:
    import jwt
    from jwt import InvalidTokenError
except Exception:
    jwt = None

    class InvalidTokenError(Exception):
        pass


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _base_url() -> str:
    # `CAMPAY_BASE_URL` is expected to be the host root (e.g. https://campay.net or https://campay.net).
    # Normalize common misconfigurations like including a trailing `/api`.
    raw = _env("CAMPAY_BASE_URL", "https://campay.net").rstrip("/")
    if raw.endswith("/api"):
        raw = raw[: -len("/api")]
    return raw.rstrip("/")

def _api_url(path: str) -> str:
    normalized_path = (path or "").lstrip("/")
    return f"{_base_url()}/{normalized_path}"


def _timeout_seconds() -> int:
    raw_timeout = _env("CAMPAY_HTTP_TIMEOUT_SECONDS", "20")
    try:
        value = int(raw_timeout)
        return value if value > 0 else 20
    except ValueError:
        return 20


def _safe_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text}


def _required_credentials_present() -> bool:
    has_access_token = bool(_env("CAMPAY_ACCESS_TOKEN"))
    has_username_password = bool(_env("CAMPAY_USERNAME") and _env("CAMPAY_PASSWORD"))
    return has_access_token or has_username_password


def get_access_token() -> dict[str, Any]:
    static_token = _env("CAMPAY_ACCESS_TOKEN")
    if static_token:
        return {
            "ok": True,
            "token": static_token,
            "token_source": "env",
            "status_code": 200,
            "data": {},
        }

    username = _env("CAMPAY_USERNAME")
    password = _env("CAMPAY_PASSWORD")
    if not username or not password:
        return {
            "ok": False,
            "token": "",
            "status_code": 0,
            "error": "Missing CamPay credentials. Set CAMPAY_ACCESS_TOKEN or CAMPAY_USERNAME + CAMPAY_PASSWORD.",
            "data": {},
        }

    headers = {"Content-Type": "application/json"}
    payload = {"username": username, "password": password}

    try:
        response = requests.post(
            _api_url("/api/token/"),
            json=payload,
            headers=headers,
            timeout=_timeout_seconds(),
        )
    except requests.RequestException as exc:
        return {
            "ok": False,
            "token": "",
            "status_code": 0,
            "error": f"Unable to request CamPay token: {exc}",
            "data": {},
        }

    data = _safe_json(response)
    token = data.get("token") if isinstance(data, dict) else ""

    if response.status_code != 200 or not token:
        return {
            "ok": False,
            "token": "",
            "status_code": response.status_code,
            "error": "CamPay token request failed.",
            "data": data,
        }

    return {
        "ok": True,
        "token": token,
        "token_source": "api",
        "status_code": response.status_code,
        "data": data,
    }


def _auth_headers(token: str) -> dict[str, str]:
    # Use the same minimal headers as the working `test_campay.py` script.
    # Some environments are sensitive to mismatched App IDs, so we omit X-App-ID.
    return {
        "Authorization": f"Token {token}",
        "Content-Type": "application/json",
    }


def request_collect(
    *,
    phone: str,
    amount: int,
    external_reference: str,
    description: str = "Boost Listing",
    external_user: str = "",
) -> dict[str, Any]:
    # print("🔥 ENTERED request_collect")
    if not _required_credentials_present():
        return {
            "ok": False,
            "status_code": 0,
            "reference": "",
            "status": "",
            "error": "Missing CamPay credentials.",
            "data": {},
        }

    token_result = get_access_token()
    if not token_result.get("ok"):
        return {
            "ok": False,
            "status_code": token_result.get("status_code", 0),
            "reference": "",
            "status": "",
            "error": token_result.get("error", "CamPay authentication failed."),
            "data": token_result.get("data", {}),
            "network_error": False,
        }

    payload = {
        "amount": str(amount),
        "currency": "XAF",
        "from": str(phone),
        "description": str(description),
        "external_reference": str(external_reference),
        "external_user": str(external_user),
    }
    logger.info(
        "campay_collect_request external_reference=%s amount=%s phone=***%s",
        external_reference,
        amount,
        str(phone)[-4:] if phone else "",
    )

    try:
        response = requests.post(
            _api_url("/api/collect/"),
            json=payload,
            headers=_auth_headers(token_result["token"]),
            timeout=_timeout_seconds(),
        )
    except requests.RequestException as exc:
        logger.warning("campay_collect_network_error external_reference=%s error=%s", external_reference, exc)
        return {
            "ok": False,
            "status_code": 0,
            "reference": "",
            "status": "",
            "error": f"Unable to submit CamPay payment request (network/DNS error): {exc}",
            "data": {},
            "network_error": True,
        }

    data = _safe_json(response)
    reference = ""
    status = "PENDING"
    if isinstance(data, dict):
        reference = str(data.get("reference") or "").strip()
        status = str(data.get("status") or status).upper().strip()

    ok = response.status_code == 200 and bool(reference)
    error = ""
    if not ok:
        if isinstance(data, dict):
            error = str(
                data.get("message") or data.get("reason") or data.get("detail") or "CamPay request payment failed."
            ).strip()
        else:
            error = "CamPay request payment failed."

    logger.info(
        "campay_collect_response external_reference=%s status_code=%s ok=%s provider_ref=%s provider_status=%s",
        external_reference,
        response.status_code,
        ok,
        reference,
        status,
    )

    return {
        "ok": ok,
        "status_code": response.status_code,
        "reference": reference,
        "status": status,
        "error": error,
        "data": data,
        "network_error": False,
    }



def get_transaction_status(reference: str) -> dict[str, Any]:
    token_result = get_access_token()
    if not token_result.get("ok"):
        return {
            "ok": False,
            "status_code": token_result.get("status_code", 0),
            "reference": reference,
            "status": "",
            "error": token_result.get("error", "CamPay authentication failed."),
            "data": token_result.get("data", {}),
            "network_error": False,
        }

    logger.info("campay_status_request provider_ref=%s", reference)

    try:
        response = requests.get(
            _api_url(f"/api/transaction/{reference}/"),
            headers=_auth_headers(token_result["token"]),
            timeout=_timeout_seconds(),
        )
    except requests.RequestException as exc:
        logger.warning("campay_status_network_error provider_ref=%s error=%s", reference, exc)
        return {
            "ok": False,
            "status_code": 0,
            "reference": reference,
            "status": "",
            "error": f"Unable to fetch CamPay transaction status (network/DNS error): {exc}",
            "data": {},
            "network_error": True,
        }

    data = _safe_json(response)
    status = ""
    resolved_reference = reference
    if isinstance(data, dict):
        status = str(data.get("status") or "").upper().strip()
        resolved_reference = str(data.get("reference") or reference)

    error = ""
    if not response.ok:
        if isinstance(data, dict):
            error = str(
                data.get("message") or data.get("reason") or data.get("detail") or "CamPay status request failed."
            ).strip()
        else:
            error = "CamPay status request failed."

    has_status = bool(status)
    logger.info(
        "campay_status_response provider_ref=%s status_code=%s provider_status=%s has_status=%s",
        reference,
        response.status_code,
        status,
        has_status,
    )

    return {
        "ok": response.ok or has_status,
        "status_code": response.status_code,
        "reference": resolved_reference,
        "status": status,
        "error": error,
        "data": data,
        "network_error": False,
    }
    


def verify_webhook_signature(payload: dict[str, Any], webhook_key: str) -> dict[str, Any]:
    signature = str(payload.get("signature") or "").strip()
    if not webhook_key:
        return {
            "ok": False,
            "error": "CAMPAY_WEBHOOK_KEY is not configured.",
            "claims": {},
        }

    if not signature:
        return {
            "ok": False,
            "error": "Signature missing in webhook payload.",
            "claims": {},
        }

    if jwt is None:
        return {
            "ok": False,
            "error": "PyJWT is not installed; signature verification is unavailable.",
            "claims": {},
        }

    try:
        header = jwt.get_unverified_header(signature)
        selected_alg = str(header.get("alg") or "").upper().strip()
        if selected_alg == "NONE":
            return {
                "ok": False,
                "error": "Insecure webhook signature algorithm.",
                "claims": {},
            }

        algorithms = [selected_alg] if selected_alg else ["HS256", "HS384", "HS512"]
        claims = jwt.decode(
            signature,
            webhook_key,
            algorithms=algorithms,
            options={
                "verify_signature": True,
                "verify_exp": False,
                "verify_nbf": False,
                "verify_iat": False,
                "verify_aud": False,
                "verify_iss": False,
            },
        )
    except InvalidTokenError as exc:
        return {
            "ok": False,
            "error": f"Invalid webhook signature: {exc}",
            "claims": {},
        }

    expected_reference = str(payload.get("reference") or "").strip()
    claim_reference = str(claims.get("reference") or "").strip() if isinstance(claims, dict) else ""
    if expected_reference and claim_reference and expected_reference != claim_reference:
        return {
            "ok": False,
            "error": "Webhook signature claims do not match transaction reference.",
            "claims": claims,
        }

    return {
        "ok": True,
        "error": "",
        "claims": claims if isinstance(claims, dict) else {},
    }

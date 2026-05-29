import requests

BASE_URL = "https://demo.campay.net"

TOKEN = os.getenv("CAMPAY_ACCESS_TOKEN", "")

payload = {
    "amount": "3",
    "currency": "XAF",
    "from": "237676210234",  # your MTN or Orange number
    "description": "Test payment"
}

headers = {
    "Authorization": f"Token {TOKEN}",
    "Content-Type": "application/json"
}

response = requests.post(
    f"{BASE_URL}/api/collect/",
    json=payload,
    headers=headers
)

print("STATUS:", response.status_code)
print("BODY:", response.text)
import requests

BASE_URL = "https://demo.campay.net"

TOKEN = "9a2762712c87bc983c1f27a2662195de675163f9"

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
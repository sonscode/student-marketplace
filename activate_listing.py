"""One-time script to activate the paid listing in the production database."""
import os
from dotenv import load_dotenv

load_dotenv()

import psycopg2

url = os.getenv("DATABASE_URL")
if not url:
    print("ERROR: DATABASE_URL not set")
    exit(1)

conn = psycopg2.connect(url)
cur = conn.cursor()

TARGET_UUID = "6db8dd6d-0ba1-4b49-9a98-64b630325591"

cur.execute(
    "SELECT p.id, p.listing_id, p.reference, p.provider_reference, p.status, p.amount "
    "FROM payments p WHERE p.provider_reference = %s",
    (TARGET_UUID,),
)
p = cur.fetchone()

if p:
    payment_id, listing_id, reference, provider_reference, status, amount = p
    print(f"Payment found: id={payment_id}, listing_id={listing_id}, status={status}, amount={amount}")

    cur.execute("UPDATE payments SET status = %s WHERE id = %s", ("SUCCESSFUL", payment_id))
    cur.execute("UPDATE listings SET is_featured = 1 WHERE id = %s", (listing_id,))
    conn.commit()
    print(f"DONE - Payment {payment_id} marked SUCCESSFUL, listing {listing_id} is now featured")
else:
    print(f"No payment found with provider_reference={TARGET_UUID}")

conn.close()
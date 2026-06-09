#!/usr/bin/env python3
import os
import sys

os.environ["SKIP_APP_INIT_DB"] = "1"

from app import get_db


def main():
    email = input("Email address: ").strip()
    if not email:
        print("Rows updated: 0")
        return 0

    conn = None
    cursor = None

    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET is_admin = ? WHERE lower(email) = lower(?)",
            (1, email),
        )
        rows_updated = cursor.rowcount or 0
        conn.commit()
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        print(f"Failed to update admin user: {exc}", file=sys.stderr)
        return 1
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

    print(f"Rows updated: {rows_updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

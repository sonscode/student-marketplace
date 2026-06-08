#!/usr/bin/env python3
import argparse
import os
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

try:
    import psycopg2
except ImportError:
    psycopg2 = None

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SQLITE_PATH = BASE_DIR / "database.db"
DEFAULT_SCHEMA_PATH = BASE_DIR / "postgres_schema.sql"
TABLE_ORDER = ("users", "listings", "payments", "reports")
TABLE_COLUMNS = {
    "users": (
        "id",
        "full_name",
        "phone",
        "password_hash",
        "email",
        "google_sub",
        "auth_provider",
        "is_admin",
        "is_active",
        "created_at",
    ),
    "listings": (
        "id",
        "title",
        "price",
        "category",
        "phone",
        "owner_phone",
        "leave_date",
        "description",
        "image",
        "is_featured",
        "view_count",
    ),
    "payments": (
        "id",
        "listing_id",
        "reference",
        "amount",
        "status",
        "phone",
        "created_at",
        "provider_reference",
    ),
    "reports": (
        "id",
        "listing_id",
        "reporter_user_id",
        "reason",
        "comment",
        "created_at",
    ),
}


def utc_now():
    return datetime.utcnow().isoformat(timespec="seconds")


def blank_to_none(value):
    if isinstance(value, str) and not value.strip():
        return None
    return value


def normalize_database_url(database_url):
    if database_url.startswith("postgres://"):
        return "postgresql://" + database_url[len("postgres://"):]
    return database_url


def parse_args():
    if load_dotenv is not None:
        load_dotenv()

    parser = argparse.ArgumentParser(description="Copy SQLite data from database.db into PostgreSQL.")
    parser.add_argument("--sqlite-path", default=str(DEFAULT_SQLITE_PATH), help="Path to the SQLite database.")
    parser.add_argument(
        "--postgres-url",
        default=os.getenv("DATABASE_URL", "").strip(),
        help="PostgreSQL connection URL. Defaults to DATABASE_URL.",
    )
    parser.add_argument("--schema-file", default=str(DEFAULT_SCHEMA_PATH), help="PostgreSQL schema SQL file.")
    parser.add_argument(
        "--truncate",
        action="store_true",
        help="Delete existing PostgreSQL rows before copying data.",
    )
    args = parser.parse_args()

    if not args.postgres_url:
        parser.error("Provide --postgres-url or set DATABASE_URL.")

    sqlite_path = Path(args.sqlite_path)
    if not sqlite_path.exists():
        parser.error(f"SQLite database not found: {sqlite_path}")

    schema_path = Path(args.schema_file)
    if not schema_path.exists():
        parser.error(f"PostgreSQL schema file not found: {schema_path}")

    return args


def sqlite_table_exists(connection, table):
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def get_source_value(row, column):
    if column in row.keys():
        return row[column]
    return None


def row_to_dict(table, row):
    values = {column: get_source_value(row, column) for column in TABLE_COLUMNS[table]}

    if values["id"] is None:
        raise ValueError(f"Source row in {table} is missing id.")

    if table == "users":
        values["email"] = blank_to_none(values["email"])
        values["google_sub"] = blank_to_none(values["google_sub"])
        values["auth_provider"] = values["auth_provider"] or "local"
        values["is_admin"] = 0 if values["is_admin"] is None else values["is_admin"]
        values["is_active"] = 1 if values["is_active"] is None else values["is_active"]
        values["created_at"] = values["created_at"] or utc_now()

    if table == "listings":
        values["owner_phone"] = values["owner_phone"] or values["phone"]
        values["is_featured"] = 0 if values["is_featured"] is None else values["is_featured"]
        values["view_count"] = 0 if values["view_count"] is None else values["view_count"]

    if table == "payments":
        legacy_reference = get_source_value(row, "reference_id")
        values["listing_id"] = 0 if values["listing_id"] is None else values["listing_id"]
        values["reference"] = values["reference"] or legacy_reference or str(uuid.uuid4())
        values["amount"] = 0 if values["amount"] is None else values["amount"]
        values["status"] = values["status"] or "PENDING"
        values["phone"] = values["phone"] or ""
        values["created_at"] = values["created_at"] or utc_now()
        values["provider_reference"] = blank_to_none(values["provider_reference"])

    if table == "reports":
        values["listing_id"] = 0 if values["listing_id"] is None else values["listing_id"]
        values["reporter_user_id"] = 0 if values["reporter_user_id"] is None else values["reporter_user_id"]
        values["reason"] = values["reason"] or "Other"
        values["comment"] = values["comment"] or ""
        values["created_at"] = values["created_at"] or utc_now()

    return values


def read_sqlite_table(connection, table):
    if not sqlite_table_exists(connection, table):
        return []

    rows = connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
    return [row_to_dict(table, row) for row in rows]


def create_postgres_schema(cursor, schema_path):
    cursor.execute(Path(schema_path).read_text(encoding="utf-8"))


def truncate_postgres_tables(cursor):
    cursor.execute("TRUNCATE TABLE reports, payments, listings, users RESTART IDENTITY")


def copy_table(cursor, table, rows):
    if not rows:
        return 0

    columns = TABLE_COLUMNS[table]
    column_sql = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    updates = ", ".join(f"{column} = EXCLUDED.{column}" for column in columns if column != "id")
    sql = f"""
        INSERT INTO {table} ({column_sql})
        VALUES ({placeholders})
        ON CONFLICT (id) DO UPDATE SET {updates}
    """
    values = [tuple(row[column] for column in columns) for row in rows]
    cursor.executemany(sql, values)
    return len(rows)


def reset_postgres_sequences(cursor):
    for table in TABLE_ORDER:
        cursor.execute(
            f"""
            SELECT setval(
                pg_get_serial_sequence('{table}', 'id'),
                COALESCE((SELECT MAX(id) FROM {table}), 1),
                (SELECT COUNT(*) > 0 FROM {table})
            )
            """
        )


def main():
    args = parse_args()

    if psycopg2 is None:
        print("psycopg2 is required. Install requirements.txt first.", file=sys.stderr)
        return 1

    sqlite_connection = sqlite3.connect(args.sqlite_path)
    sqlite_connection.row_factory = sqlite3.Row

    copied_counts = {}
    try:
        postgres_connection = psycopg2.connect(normalize_database_url(args.postgres_url))
        try:
            with postgres_connection:
                with postgres_connection.cursor() as cursor:
                    create_postgres_schema(cursor, args.schema_file)
                    if args.truncate:
                        truncate_postgres_tables(cursor)

                    for table in TABLE_ORDER:
                        rows = read_sqlite_table(sqlite_connection, table)
                        copied_counts[table] = copy_table(cursor, table, rows)

                    reset_postgres_sequences(cursor)
        finally:
            postgres_connection.close()
    finally:
        sqlite_connection.close()

    for table in TABLE_ORDER:
        print(f"{table}: copied {copied_counts.get(table, 0)} row(s)")
    print("Migration complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    full_name TEXT NOT NULL,
    phone TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    email TEXT,
    google_sub TEXT,
    auth_provider TEXT NOT NULL DEFAULT 'local',
    is_admin INTEGER DEFAULT 0,
    is_active INTEGER DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS listings (
    id SERIAL PRIMARY KEY,
    title TEXT,
    price TEXT,
    category TEXT,
    phone TEXT,
    owner_phone TEXT,
    leave_date TEXT,
    description TEXT,
    image TEXT,
    is_featured INTEGER DEFAULT 0,
    view_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS payments (
    id SERIAL PRIMARY KEY,
    listing_id INTEGER NOT NULL,
    reference TEXT NOT NULL UNIQUE,
    amount INTEGER NOT NULL,
    status TEXT NOT NULL,
    phone TEXT,
    created_at TEXT NOT NULL,
    provider_reference TEXT
);

CREATE TABLE IF NOT EXISTS reports (
    id SERIAL PRIMARY KEY,
    listing_id INTEGER NOT NULL,
    reporter_user_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    comment TEXT,
    created_at TEXT NOT NULL
);

ALTER TABLE listings ADD COLUMN IF NOT EXISTS title TEXT;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS price TEXT;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS category TEXT;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS phone TEXT;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS owner_phone TEXT;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS leave_date TEXT;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS image TEXT;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS is_featured INTEGER DEFAULT 0;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS view_count INTEGER DEFAULT 0;

ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS google_sub TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_provider TEXT DEFAULT 'local';
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin INTEGER DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active INTEGER DEFAULT 1;
ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TEXT;

ALTER TABLE payments ADD COLUMN IF NOT EXISTS listing_id INTEGER;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS reference TEXT;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS amount INTEGER DEFAULT 0;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'PENDING';
ALTER TABLE payments ADD COLUMN IF NOT EXISTS phone TEXT;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS created_at TEXT;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS provider_reference TEXT;

ALTER TABLE reports ADD COLUMN IF NOT EXISTS listing_id INTEGER;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS reporter_user_id INTEGER;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS reason TEXT;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS comment TEXT;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS created_at TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone_unique ON users(phone);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique ON users(email);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_sub_unique ON users(google_sub);
CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_reference_unique ON payments(reference);
CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_provider_reference_unique ON payments(provider_reference);
CREATE INDEX IF NOT EXISTS idx_payments_listing_id ON payments(listing_id);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
CREATE INDEX IF NOT EXISTS idx_reports_listing_id ON reports(listing_id);
CREATE INDEX IF NOT EXISTS idx_reports_reporter_user_id ON reports(reporter_user_id);
CREATE INDEX IF NOT EXISTS idx_reports_created_at ON reports(created_at);

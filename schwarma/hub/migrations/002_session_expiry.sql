-- Migration 002: Add API key expiry and rotation support
--
-- Adds expires_at to sessions table for API key TTL enforcement,
-- and a key rotation tracking column.

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS rotated_from TEXT;

-- Index for efficient expired-session cleanup
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions (expires_at)
    WHERE expires_at IS NOT NULL;

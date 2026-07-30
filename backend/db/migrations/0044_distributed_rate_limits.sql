BEGIN;

CREATE TABLE IF NOT EXISTS api_rate_limit_windows (
    identity_hash   char(64)    NOT NULL,
    endpoint_bucket text        NOT NULL,
    window_start    timestamptz NOT NULL,
    request_count   integer     NOT NULL CHECK (request_count > 0),
    expires_at      timestamptz NOT NULL,
    PRIMARY KEY (identity_hash, endpoint_bucket, window_start)
);

CREATE INDEX IF NOT EXISTS idx_api_rate_limit_windows_expiry
    ON api_rate_limit_windows (expires_at);

REVOKE ALL ON api_rate_limit_windows FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON api_rate_limit_windows TO oracle_app_login;

COMMIT;

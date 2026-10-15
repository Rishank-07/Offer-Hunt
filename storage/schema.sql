-- OFFERHUNT SQLite Persistence Schema (Milestone M2, Section 15)
-- Foreign keys must be enabled per connection via PRAGMA foreign_keys = ON;

PRAGMA foreign_keys = ON;

-- 1. Schema Versioning Table
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at_utc TEXT NOT NULL,
    description TEXT
);

-- 2. Execution Runs Table
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('INITIALIZING', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED')),
    started_at_utc TEXT NOT NULL,
    completed_at_utc TEXT,
    metadata TEXT -- JSON encoded run config / environment
);

-- 3. Steps Table
CREATE TABLE IF NOT EXISTS steps (
    step_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    step_order INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('READY', 'RUNNING', 'EXECUTED', 'VERIFIED', 'RETRY_WAIT', 'UNRESOLVED', 'NEEDS_APPROVAL', 'FAILED')),
    execution_status TEXT NOT NULL CHECK(execution_status IN ('NOT_STARTED', 'IN_PROGRESS', 'COMPLETED', 'FAILED', 'CANCELLED')),
    verification_status TEXT NOT NULL CHECK(verification_status IN ('UNVERIFIED', 'PASSED', 'FAILED', 'REQUIRES_RECONCILIATION')),
    input_payload TEXT, -- JSON
    output_payload TEXT, -- JSON
    error_payload TEXT, -- JSON
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    UNIQUE(run_id, step_order)
);

-- 4. Tool Execution Attempts Table
CREATE TABLE IF NOT EXISTS tool_attempts (
    attempt_id TEXT PRIMARY KEY,
    step_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    parameters TEXT, -- JSON
    result TEXT, -- JSON
    error TEXT, -- JSON
    retry_count INTEGER NOT NULL DEFAULT 0,
    started_at_utc TEXT NOT NULL,
    completed_at_utc TEXT,
    FOREIGN KEY(step_id) REFERENCES steps(step_id) ON DELETE CASCADE
);

-- 5. Evidence & Verification Artifacts Table
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    step_id TEXT NOT NULL,
    evidence_type TEXT NOT NULL, -- e.g. web_snippet, screenshot, checksum, diff
    content TEXT,
    source_url TEXT,
    hash TEXT,
    captured_at_utc TEXT NOT NULL,
    FOREIGN KEY(step_id) REFERENCES steps(step_id) ON DELETE CASCADE
);

-- 6. Approvals Table
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    step_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    payload TEXT, -- JSON
    status TEXT NOT NULL CHECK(status IN ('PENDING', 'APPROVED', 'REJECTED', 'EXPIRED')),
    requested_at_utc TEXT NOT NULL,
    responded_at_utc TEXT,
    response_notes TEXT,
    FOREIGN KEY(step_id) REFERENCES steps(step_id) ON DELETE CASCADE
);

-- 7. Artifacts Table
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    step_id TEXT,
    artifact_type TEXT NOT NULL, -- e.g. spreadsheet, eml_draft, log
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    metadata TEXT, -- JSON
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY(step_id) REFERENCES steps(step_id) ON DELETE SET NULL
);

-- 8. Audit Events Table
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    step_id TEXT,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')),
    payload TEXT, -- JSON
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY(step_id) REFERENCES steps(step_id) ON DELETE SET NULL
);

-- 9. Metrics Runs Table
CREATE TABLE IF NOT EXISTS metrics_runs (
    metric_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    metric_value REAL NOT NULL,
    recorded_at_utc TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

-- Indexes for performance and foreign key traversals
CREATE INDEX IF NOT EXISTS idx_steps_run_id ON steps(run_id);
CREATE INDEX IF NOT EXISTS idx_steps_state ON steps(state);
CREATE INDEX IF NOT EXISTS idx_steps_execution_status ON steps(execution_status);
CREATE INDEX IF NOT EXISTS idx_tool_attempts_step_id ON tool_attempts(step_id);
CREATE INDEX IF NOT EXISTS idx_evidence_step_id ON evidence(step_id);
CREATE INDEX IF NOT EXISTS idx_approvals_step_id ON approvals(step_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_run_id ON artifacts(run_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_step_id ON artifacts(step_id);
CREATE INDEX IF NOT EXISTS idx_events_run_id ON events(run_id);
CREATE INDEX IF NOT EXISTS idx_events_step_id ON events(step_id);
CREATE INDEX IF NOT EXISTS idx_metrics_run_id ON metrics_runs(run_id);

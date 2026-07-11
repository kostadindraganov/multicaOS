ALTER TABLE runtime_profile DROP CONSTRAINT IF EXISTS runtime_profile_protocol_family_check;

-- Widen the whitelist to include Omnigent (`omnigent`), the multi-harness
-- agent orchestrator driven over its local HTTP+SSE server API. NOT VALID
-- mirrors migrations 126/134/136 so historical tolerated rows do not block
-- the upgrade.
ALTER TABLE runtime_profile ADD CONSTRAINT runtime_profile_protocol_family_check
    CHECK (protocol_family IN (
        'claude',
        'codebuddy',
        'codex',
        'copilot',
        'opencode',
        'openclaw',
        'hermes',
        'pi',
        'cursor',
        'kimi',
        'kiro',
        'antigravity',
        'qoder',
        'traecli',
        'omnigent'
    )) NOT VALID;

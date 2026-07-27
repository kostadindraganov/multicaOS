ALTER TABLE runtime_profile DROP CONSTRAINT IF EXISTS runtime_profile_protocol_family_check;

-- Widen the whitelist to include Omnigent (`omnigent`), the multi-harness
-- agent orchestrator driven over its local HTTP+SSE server API. Extends the
-- migration 202 shape (which added `qwen`), so deveco/grok/qwen stay in the
-- whitelist. NOT VALID mirrors the prior family additions so historical
-- tolerated rows do not block the upgrade.
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
        'deveco',
        'grok',
        'qwen',
        'omnigent'
    )) NOT VALID;

# Codex activity and quota

Token activity detects Codex use; the rate-limit API supplies plan quota. There is no token-to-quota conversion.

## Sources verified in this installation

- `providers.fetch_chatgpt()` reads `https://chatgpt.com/backend-api/wham/usage` using the existing authentication path. The adapter expects `rate_limit.primary_window/secondary_window`, `used_percent`, `limit_window_seconds`, and `reset_at`. After restart, the normal worker successfully returned both quota windows; raw float values and separately rounded display values were recorded in the debug log. The full HTTP body was not dumped.
- Local session JSONL inspection found `type: event_msg`, `payload.type: token_count`, `info.total_token_usage`, `info.last_token_usage`, and `model_context_window`. Its `rate_limits` object contains `primary`, `secondary`, `credits`, `limit_id`, `limit_name`, `individual_limit`, `spend_control_reached`, `plan_type`, and `rate_limit_reached_type`.
- Local rate-limit events request an HTTP refresh; their core/event schema is not parsed as an HTTP payload. App-server is not started or polled by this change. App-server rounding behavior was not independently measured.
- Existing `QuotaBar.used_percent` and `remaining_percent` remain floats. Integer formatting is only presentation. Existing credits and reset-credit handling is unchanged.

## Scheduling

`CodexActivityMonitor` checks local session append data at most once per second. A fresh cumulative token increase or `task_complete` enters FAST mode. Repeated identical token totals do not prolong it. FAST requests wait until two seconds after the previous request start, with one worker per provider. A slow response does not queue a second worker; the next start happens at max(now, start + 2s).

After 12 seconds without activity, NORMAL resumes: existing 20/30-second scheduling, 300 seconds for exhausted quota, and existing error backoff. Quota changes never extend GPT FAST mode. A new rate-limit event can independently request a refresh without entering FAST mode. Cursor usage drops still enter a 60-second FAST window, but polling is also 2 seconds start-to-start instead of immediate.

The first scan seeds existing files without triggering effects. Discovery covers today/yesterday and up to 64 recently modified tracked sessions, with reads limited to 64 KiB per file per scan. Older untracked sessions and remote/cloud sessions are not guaranteed to be detected; periodic quota polling remains the fallback. Malformed JSON, partial writes, truncated files, and inaccessible files do not interrupt quota polling. Only event metadata and token totals are retained, never conversation text.

## UI and diagnostics

Fresh activity keeps the GPT bar thick and the shimmer looping until 12 seconds of inactivity. Bar width continues to interpolate actual raw quota changes only. Activity does not synthesize or decrement quota.

`%APPDATA%/AiUsageWidget/activity-debug.log` records activity, total changes, FAST/NORMAL transitions, and raw/display quota values. Rotation limits each file to 256 KiB plus one backup. No credentials, paths, prompts, or replies are logged.

## Validation

Synthetic local events cover small activity with unchanged quota, repeated activity, inactivity expiry, duplicate totals, partial writes, malformed lines, truncation, inaccessible source, and turn completion. Scheduler tests cover unchanged quota during FAST and return to NORMAL. Existing provider, runtime, updater, shimmer, and UI suites remain in use. Synthetic tests do not prove detection for every Codex version or remote session.

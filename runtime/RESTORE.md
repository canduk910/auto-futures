# Runtime Restore Specification

Goal: Allow trader process to bootstrap from persisted runtime files and JSON settings managed by the UI.

1. Files to restore
   - runtime/status.json: last known operational status
   - runtime/ai_history.jsonl: AI advisory history
   - runtime/close_history.jsonl: position close entries
   - runtime/settings.json: UI-managed trading/runtime configuration

2. Loading responsibilities
   - service_runner initializes caches (WsCache) but no disk restore
   - auto_future_trader only appends new entries
   - config_store.apply_runtime_settings_to_env() loads runtime/settings.json and populates os.environ before other modules read values

3. Restore flow
   - docker-entrypoint.py calls config_store.apply_runtime_settings_to_env() and runtime_sync.safe_download() (when enabled) before launching processes
   - runtime/status_store reads status.json/AI history/close history directly when serving the UI

4. TODO
   - Implement runtime_restore module
   - Add tests ensuring no double counting

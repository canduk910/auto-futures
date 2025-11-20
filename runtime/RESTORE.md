# Runtime Restore Specification

Goal: Allow trader process to bootstrap from persisted runtime files.

1. Files to restore
   - runtime/status.json: last known operational status
   - runtime/ai_history.jsonl: AI advisory history
   - runtime/close_history.jsonl: position close entries
   - runtime/ai_history.jsonl: ???

2. Loading responsibilities
   - service_runner initializes caches (WsCache) but no disk restore
   - auto_future_trader only appends new entries

3. Proposed restore flow
   - At service startup, call new runtime_restore.load_all()
   - Update status_store to accept seeds for events/orders/positions
   - Provide CLI flag to skip restore (default true in cloud)

4. TODO
   - Implement runtime_restore module
   - Add tests ensuring no double counting


from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pandas as pd

import bot_runtime
import config


def test_engine_runtime_status_live_from_snapshot_and_log(tmp_path, monkeypatch):
    db_dir = tmp_path
    monkeypatch.setattr(config, "BASE_DIR", db_dir)
    monkeypatch.setattr(config, "RUNTIME_SNAPSHOT_FILE", str(db_dir / ".bot_runtime_snapshot.json"))
    monkeypatch.setattr(bot_runtime, "RUNTIME_SNAPSHOT_PATH", str(db_dir / ".bot_runtime_snapshot.json"))
    monkeypatch.setattr(bot_runtime, "INSTANCE_LOCK_PATH", str(db_dir / ".bot_instance.lock"))
    monkeypatch.setattr(config, "LOOP_SLEEP_SECONDS", 5)

    with open(db_dir / ".bot_instance.lock", "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))

    now = datetime.now(timezone.utc).isoformat()
    bot_runtime.write_runtime_snapshot(
        {
            "pid": os.getpid(),
            "running": True,
            "updated_at": now,
            "connection_degraded": False,
            "box": {"valid": True, "box_top": 110.0, "box_bottom": 90.0, "middle_line": 100.0},
        }
    )
    log = pd.DataFrame(
        {
            "Timestamp": [now],
            "Action": ["HOLD"],
            "Event": ["SCAN"],
            "Open_Position": ["FLAT"],
        }
    )
    status = bot_runtime.engine_runtime_status(log)
    assert status["running"] is True
    assert status["process_alive"] is True
    assert status["snapshot"]["box"]["box_top"] == 110.0


def test_darvas_box_stats_prefers_runtime_snapshot(tmp_path, monkeypatch):
    import dashboard_stats

    db_dir = tmp_path
    monkeypatch.setattr(config, "BASE_DIR", db_dir)
    runtime = {
        "running": True,
        "snapshot": {
            "box": {
                "valid": True,
                "active_box_number": 3,
                "box_top": 120.0,
                "box_bottom": 100.0,
                "middle_line": 110.0,
                "box_height": 20.0,
                "breakout": "LONG",
                "prev_day": "2026-07-07",
                "reason": "test",
            }
        },
    }
    stats = dashboard_stats.darvas_box_stats(None, runtime=runtime)
    assert stats["valid"] is True
    assert stats["active_box_number"] == 3
    assert stats["box_top"] == 120.0
    assert stats["breakout"] == "LONG"

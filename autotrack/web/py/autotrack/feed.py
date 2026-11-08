"""
feed.py - the shared message feed ("message-driven operations").

Every zone change, RFID read, photo verdict, alert, tag removal and tag return becomes one
small JSON message, e.g.

  {"seq": 812, "t": 1374, "type": "ALERT_RAISED", "bag": "B0181", "status": "WRONG AIRCRAFT",
   "text": "Loader GA4 serves AC 857 but B0181 is booked on AC 1234"}

Messages are kept in memory for the dashboards and saved to SQLite (data/events.db), so
another system (Brock SmartBag, airline BSM/BPM messages) could subscribe to the same feed.
"""

import json
import sqlite3
import threading
import time
from collections import deque

from .layout import ROOT


class Feed:
    def __init__(self, db_path=ROOT / "data" / "events.db", keep=5000, use_db=True):
        self.events = deque(maxlen=keep)
        self.seq = 0
        self.run_id = int(time.time())
        self.listeners = []          # callables, e.g. the server's live stream
        self._lock = threading.Lock()
        self._pending = []
        self.db = None
        if use_db:
            self.db = sqlite3.connect(str(db_path), check_same_thread=False)
            self.db.execute("CREATE TABLE IF NOT EXISTS events (run_id INTEGER, seq INTEGER, t INTEGER, "
                            "type TEXT, bag TEXT, json TEXT)")
            self.db.commit()

    def publish(self, t, type_, text, **fields):
        with self._lock:
            self.seq += 1
            ev = {"seq": self.seq, "t": t, "type": type_, "text": text, **fields}
            self.events.append(ev)
            self._pending.append(ev)
        for fn in list(self.listeners):
            try:
                fn(ev)
            except Exception as e:  # never let one listener break the feed, but say so
                print(f"[feed] listener error: {e!r}", flush=True)
        return ev

    def flush(self):
        """Write queued messages to SQLite (called about once a second)."""
        if not self.db or not self._pending:
            return
        with self._lock:
            rows, self._pending = self._pending, []
        self.db.executemany("INSERT INTO events VALUES (?,?,?,?,?,?)",
                            [(self.run_id, e["seq"], e["t"], e["type"], e.get("bag"), json.dumps(e)) for e in rows])
        self.db.commit()

    def since(self, seq, limit=300):
        with self._lock:
            return [e for e in self.events if e["seq"] > seq][-limit:]

    def reset(self):
        self.flush()
        with self._lock:
            self.events.clear()
            self.seq = 0
            self.run_id = int(time.time() * 1000)

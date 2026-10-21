#!/usr/bin/env python3
"""自检辅助: 返回last_run年龄(分钟)"""
import sqlite3
try:
    c = sqlite3.connect('file:$(dirname "$0")/ledger.db?mode=ro', uri=True)
    r = c.execute("SELECT v FROM meta WHERE k='last_run'").fetchone()
    print(int(r[0]) if r else 0)
except Exception:
    print(0)

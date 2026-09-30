"""Persist analyses and subsequent observations. No positions, fills or P&L."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .scanner import ENTRY_DECISIONS, MAX_AGE, number


class SignalHistory:
    def __init__(self, path):
        self.path=Path(path)

    def publish(self, report):
        now=number(report["completed_at"])
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(self.path,timeout=5)) as db, db:
            db.row_factory=sqlite3.Row
            db.executescript("""
                CREATE TABLE IF NOT EXISTS analyses (
                  row_id TEXT NOT NULL, snapshot_at REAL NOT NULL, observed_at REAL NOT NULL,
                  price REAL NOT NULL, decision TEXT NOT NULL, score REAL NOT NULL,
                  PRIMARY KEY(row_id,snapshot_at));
                CREATE INDEX IF NOT EXISTS analyses_date ON analyses(snapshot_at);
                CREATE TABLE IF NOT EXISTS signals (
                  signal_id TEXT PRIMARY KEY, row_id TEXT NOT NULL, symbol TEXT NOT NULL,
                  source TEXT NOT NULL, side TEXT NOT NULL, recorded_at REAL NOT NULL,
                  candle_at REAL NOT NULL, entry REAL NOT NULL, invalidation REAL NOT NULL,
                  target REAL NOT NULL, decision TEXT NOT NULL, checks TEXT NOT NULL,
                  progress TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS signals_row ON signals(row_id,recorded_at);
            """)
            valid={}
            for row in report.get("candidates",[]):
                try:
                    if report["status"]!="ok" or row.get("analysis_ok") is False or not 0<=now-number(row["observed_at"])<=MAX_AGE: continue
                    price=number(row["price"],positive=True)
                    valid[row["id"]]=row
                    db.execute("INSERT OR IGNORE INTO analyses VALUES (?,?,?,?,?,?)",
                               (row["id"],now,row["observed_at"],price,row["decision"],row["score"]))
                    for scenario in row.get("scenarios",[]):
                        if scenario["decision"] not in ENTRY_DECISIONS: continue
                        # Each side/candle hypothesis is recorded once, with immutable original levels.
                        key=f"{row['id']}:{scenario['side']}:{row['candle_at']:.3f}"
                        levels=scenario["levels"]
                        if not all(c["passed"] for c in scenario["checks"]): continue
                        entry=number(levels["reference_entry"],positive=True)
                        invalidation=number(levels["invalidation"],positive=True)
                        target=number(levels["reference_target"],positive=True)
                        side=scenario["side"]
                        if not (target<entry<invalidation if side=="short" else invalidation<entry<target): continue
                        db.execute("INSERT OR IGNORE INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   (key,row["id"],row["symbol"],row["source"],side,row["observed_at"],row["candle_at"],
                                    entry,invalidation,target,scenario["decision"],json.dumps(scenario["checks"],ensure_ascii=False),
                                    json.dumps(dict(horizons={},max_favorable_pct=0,max_adverse_pct=0,levels_state="Sin nivel observado",last_observed_at=row["observed_at"]))))
                except (ValueError,KeyError,TypeError):
                    continue
            for signal in db.execute("SELECT * FROM signals WHERE recorded_at>=?",(now-25*3600,)).fetchall():
                row=valid.get(signal["row_id"])
                if not row or row["observed_at"]<=signal["recorded_at"]: continue
                progress=json.loads(signal["progress"])
                sign=-1 if signal["side"]=="short" else 1
                move=sign*(row["price"]/signal["entry"]-1)*100
                progress["max_favorable_pct"]=max(progress["max_favorable_pct"],move)
                progress["max_adverse_pct"]=max(progress["max_adverse_pct"],-move)
                age=row["observed_at"]-signal["recorded_at"]
                for horizon,hours in [("1h",1),("4h",4),("24h",24)]:
                    if hours*3600<=age<=hours*3600+MAX_AGE and horizon not in progress["horizons"]:
                        progress["horizons"][horizon]=dict(move_pct=round(move,4),observed_at=row["observed_at"])
                # Whole closed candles after the observation can prove a level was touched.
                # A candle touching both levels cannot establish their order.
                previous=progress.get("last_candle_at",signal["recorded_at"])
                for candle in row.get("chart",{}).get("15m",[]):
                    if candle["time"]<signal["recorded_at"] or candle["close_at"]<=previous: continue
                    if candle["close_at"]>now: continue
                    favorable=(candle["high"]/signal["entry"]-1)*100 if sign==1 else (1-candle["low"]/signal["entry"])*100
                    adverse=(1-candle["low"]/signal["entry"])*100 if sign==1 else (candle["high"]/signal["entry"]-1)*100
                    progress["max_favorable_pct"]=max(progress["max_favorable_pct"],favorable)
                    progress["max_adverse_pct"]=max(progress["max_adverse_pct"],adverse)
                    target=candle["high"]>=signal["target"] if sign==1 else candle["low"]<=signal["target"]
                    invalid=candle["low"]<=signal["invalidation"] if sign==1 else candle["high"]>=signal["invalidation"]
                    if progress["levels_state"]=="Sin nivel observado":
                        if target and invalid: progress["levels_state"]="Ambos niveles en una vela; orden desconocido"
                        elif target: progress["levels_state"]="Objetivo observado"
                        elif invalid: progress["levels_state"]="Invalidación observada"
                    progress["last_candle_at"]=candle["close_at"]
                progress["last_observed_at"]=row["observed_at"]
                db.execute("UPDATE signals SET progress=? WHERE signal_id=?",(json.dumps(progress),signal["signal_id"]))
            # Retention concerns newly generated observations; legacy trading databases are untouched.
            db.execute("DELETE FROM analyses WHERE snapshot_at<?",(now-30*86400,))
            for row in report.get("candidates",[]):
                row["history"]=[dict(item) for item in db.execute("SELECT observed_at,price,decision,score FROM analyses WHERE row_id=? ORDER BY snapshot_at DESC LIMIT 5",(row["id"],))]
                signals=db.execute("SELECT * FROM signals WHERE row_id=? ORDER BY recorded_at DESC LIMIT 5",(row["id"],)).fetchall()
                row["signal_reviews"]=[self.public_signal(s) for s in signals]
            counts=db.execute("SELECT COUNT(*) FROM analyses").fetchone()[0]
            recent=db.execute("SELECT * FROM signals ORDER BY recorded_at DESC LIMIT 20").fetchall()
            total=db.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
            first=db.execute("SELECT MIN(snapshot_at) FROM analyses").fetchone()[0]
            return dict(status="ok",observations=counts,signals=total,started_at=first,
                        recent=[self.public_signal(s) for s in recent],
                        note="Observaciones de precio y niveles, sin órdenes, posiciones, fills ni P&L. Variación bruta sin costes; cobertura de excursiones parcial. Revisiones 1h/4h/24h toleran hasta 15 min; datos faltantes no son resultados.")

    @staticmethod
    def public_signal(signal):
        return {**{k:signal[k] for k in ("signal_id","row_id","symbol","source","side","recorded_at","entry","invalidation","target","decision")},
                "progress":json.loads(signal["progress"])}

"""Daily, idempotent research isolated from the order-management process."""
import json
import math
import os
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import load_config
from .storage import ProcessLock, _invalid_constant

BOGOTA = timezone(timedelta(hours=-5), "America/Bogota")


def day_bounds(day):
    parsed = date.fromisoformat(day)
    start = datetime.combine(parsed, datetime.min.time(), BOGOTA)
    return start.timestamp(), (start+timedelta(days=1)).timestamp()


def next_review_at(now, review_time="00:10"):
    local = datetime.fromtimestamp(now, BOGOTA)
    hour, minute = map(int, review_time.split(":"))
    following = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if following.timestamp() <= now:
        following += timedelta(days=1)
    return following.isoformat()


def reviews_path(state_path):
    return Path(state_path).with_suffix(".reviews.sqlite3")


class ReviewStore:
    def __init__(self, state_path):
        path = reviews_path(state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=3)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS reviews (day TEXT PRIMARY KEY, status TEXT, updated REAL, report TEXT, error TEXT)")
        self.db.commit()

    def get(self, day):
        row = self.db.execute("SELECT status,updated,report,error FROM reviews WHERE day=?", (day,)).fetchone()
        return dict(status=row[0], updated=row[1], report=json.loads(row[2], parse_constant=_invalid_constant) if row[2] else None, error=row[3]) if row else None

    def claim(self, day, now, lease_seconds=660):
        """Reserve dispatch atomically so two runners do not spawn two workers."""
        try:
            self.db.execute("BEGIN IMMEDIATE")
            previous = self.get(day)
            if previous and (previous["status"] == "completed" or
                             (previous["status"] in ("dispatched", "running") and now-previous["updated"] < lease_seconds)):
                self.db.rollback()
                return False
            self.db.execute("INSERT INTO reviews VALUES(?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET status=excluded.status,updated=excluded.updated,report=NULL,error=NULL",
                            (day, "dispatched", now, None, None))
            self.db.commit()
            return True
        except Exception:
            self.db.rollback()
            raise

    def put(self, day, status, report=None, error=None):
        with self.db:
            self.db.execute("INSERT INTO reviews VALUES(?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET status=excluded.status,updated=excluded.updated,report=excluded.report,error=excluded.error",
                            (day, status, time.time(), json.dumps(report, ensure_ascii=False, allow_nan=False) if report else None, error))

    def completed(self):
        return {r[0] for r in self.db.execute("SELECT day FROM reviews WHERE status='completed'")}

    def latest(self):
        row = self.db.execute("SELECT report FROM reviews WHERE status='completed' ORDER BY day DESC LIMIT 1").fetchone()
        return json.loads(row[0], parse_constant=_invalid_constant) if row else None

    def close(self):
        self.db.close()


def read_history(state_path, cutoff):
    """One consistent read transaction; never acquire the runtime writer lock."""
    if type(cutoff) not in (int, float) or not math.isfinite(cutoff):
        raise ValueError("El corte temporal debe ser finito")
    connection = sqlite3.connect(Path(state_path).resolve().as_uri()+"?mode=ro", uri=True, timeout=3)
    try:
        connection.execute("BEGIN")
        row = connection.execute("SELECT data FROM state WHERE id=1").fetchone()
        if not row:
            raise ValueError("No hay historial para revisar")
        state = json.loads(row[0], parse_constant=_invalid_constant)
        events = [dict(id=r[0], ts=r[1], kind=r[2], data=json.loads(r[3], parse_constant=_invalid_constant)) for r in connection.execute(
            "SELECT id,ts,kind,data FROM events WHERE ts < ? ORDER BY id", (cutoff,))]
        return state, events
    finally:
        connection.close()


def run_daily_review(config, state_path, review_date=None, *, preview=False, now=None, output_dir=None):
    from .daily_analysis import ResearchValidationAgent
    now = time.time() if now is None else now
    if type(now) not in (int, float) or not math.isfinite(now):
        raise ValueError("La hora del informe debe ser finita")
    today = datetime.fromtimestamp(now, BOGOTA).date()
    review_date = review_date or (today if preview else today-timedelta(days=1)).isoformat()
    _, end = day_bounds(review_date)
    if not preview and end > now:
        raise ValueError("El dia aun no termino; usa --preview para un informe parcial")
    if day_bounds(review_date)[0] > now:
        raise ValueError("No se revisan fechas futuras")
    cutoff = min(end, now) if preview else end
    output = Path(output_dir or f"reports/{Path(state_path).stem}/reviews")
    output.mkdir(parents=True, exist_ok=True)
    suffix = "-preview" if preview else ""
    with ProcessLock(reviews_path(state_path).with_suffix(".lock")):
        db = ReviewStore(state_path)
        try:
            previous = db.get(review_date)
            if not preview and previous and previous["status"] == "completed":
                return previous["report"]
            if not preview:
                db.put(review_date, "running")
            try:
                state, events = read_history(state_path, cutoff)
                from .governance import effective_config
                active_config = effective_config(config, state)
                report = ResearchValidationAgent(active_config).review(state, events, review_date, cutoff)
                report.update(preview=preview, generated_at=datetime.fromtimestamp(now, timezone.utc).isoformat(),
                              status="preview" if preview else "completed", next_review_at=next_review_at(now, config.daily_review_time),
                              source_state=Path(state_path).name, mode=state["mode"],
                              source_version=state.get("strategy", {}).get("version", 1),
                              history_observed_at=datetime.fromtimestamp(now, timezone.utc).isoformat())
                report.setdefault("limitations", []).append("Correcciones contables tardias reflejan lo conocido al generar el informe; no se reconstruye una copia historica de cada fila.")
                destination = output/(review_date+suffix+".json")
                temporary = destination.with_suffix(".tmp")
                temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
                temporary.replace(destination)
                if not preview:
                    db.put(review_date, "completed", report)
                return report
            except Exception as exc:
                if not preview:
                    db.put(review_date, "failed", error=type(exc).__name__)
                raise
        finally:
            db.close()


class DailyReviewRunner:
    """Poll a hidden subprocess; all trade execution stays in the main loop."""
    def __init__(self, config, state_path, config_path="config.toml"):
        self.config = config
        self.state_path = Path(state_path).resolve()
        self.config_path = Path(config_path).resolve()
        self.process = None
        self.process_started = 0
        self.running_date = None
        self.next_attempt = 0
        self.last_error = None
        self.terminate_requested = False

    def poll(self, state, now=None):
        now = time.time() if now is None else now
        if not self.config.daily_review_enabled:
            return None
        if self.process is not None:
            if self.process.poll() is None:
                try:
                    if now-self.process_started > 630:
                        self.process.kill()
                    elif now-self.process_started > 600 and not self.terminate_requested:
                        self.process.terminate()
                        self.terminate_requested = True
                        self.last_error = "review_timeout"
                        self.next_attempt = now+300
                except OSError:
                    self.last_error = "review_worker_unavailable"
                return None
            if self.process.returncode:
                self.last_error = "review_worker_failed"
                self.next_attempt = max(self.next_attempt, now+300)
            else:
                self.last_error = None
            self.process = None
            self.terminate_requested = False
        db = None
        try:
            db = ReviewStore(self.state_path)
            latest = db.latest()
            last_date = state.get("daily_review", {}).get("date")
            if latest and (last_date is None or latest["date"] > last_date):
                return latest
            if now < self.next_attempt:
                return None
            local = datetime.fromtimestamp(now, BOGOTA)
            hour, minute = map(int, self.config.daily_review_time.split(":"))
            last_due = local.date()-timedelta(days=1 if (local.hour, local.minute) >= (hour, minute) else 2)
            first = datetime.fromtimestamp(state["created_at"], BOGOTA).date()
            completed = db.completed()
            missing = first
            while missing <= last_due and missing.isoformat() in completed:
                missing += timedelta(days=1)
            if missing > last_due:
                return None
            if not db.claim(missing.isoformat(), now):
                return None
            self.running_date = missing.isoformat()
            command = [sys.executable, "-m", "bot", "daily-review", "--config", str(self.config_path),
                       "--mode", self.config.mode, "--state", str(self.state_path), "--date", self.running_date]
            options = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           cwd=str(self.config_path.parent))
            if os.name == "nt":
                options["creationflags"] = subprocess.CREATE_NO_WINDOW
            try:
                self.process = subprocess.Popen(command, **options)
            except OSError:
                self.last_error = "review_worker_unavailable"
                self.next_attempt = now+300
                db.put(self.running_date, "failed", error=self.last_error)
                return None
            self.process_started = now
            self.next_attempt = now+300
            return None
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
            self.last_error = "review_storage_unavailable"
            self.next_attempt = now+300
            return None
        finally:
            if db is not None:
                db.close()

    def status(self, now=None):
        return dict(enabled=self.config.daily_review_enabled, running=self.process is not None and self.process.poll() is None,
                    review_date=self.running_date, error=self.last_error,
                    next_review_at=next_review_at(time.time() if now is None else now, self.config.daily_review_time))

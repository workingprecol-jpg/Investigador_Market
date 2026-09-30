import json
import os
import sqlite3
import time
from pathlib import Path


def _invalid_constant(value):
    raise ValueError(f"JSON persistido contiene un numero no finito: {value}")


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, data TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts REAL, kind TEXT, data TEXT)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS news_articles (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, url TEXT NOT NULL,
            title TEXT NOT NULL, published_at REAL NOT NULL,
            first_seen_at REAL NOT NULL, last_seen_at REAL NOT NULL,
            symbols TEXT NOT NULL)""")
        self.db.commit()

    def load(self):
        row = self.db.execute("SELECT data FROM state WHERE id=1").fetchone()
        return json.loads(row[0], parse_constant=_invalid_constant) if row else None

    def save(self, state, kind=None, data=None):
        payload = json.dumps(state, allow_nan=False)
        with self.db:
            self.db.execute("INSERT INTO state VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data", (payload,))
            if kind:
                self.db.execute("INSERT INTO events(ts,kind,data) VALUES(?,?,?)", (time.time(), kind, json.dumps(data, allow_nan=False)))

    def events(self, limit=30):
        return [dict(ts=r[0], kind=r[1], data=json.loads(r[2], parse_constant=_invalid_constant)) for r in self.db.execute(
            "SELECT ts,kind,data FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    def record_news(self, articles, observed_at):
        """Retain the first observation of each headline version for point-in-time review."""
        with self.db:
            for article in articles:
                self.db.execute("""INSERT INTO news_articles
                    (id, source, url, title, published_at, first_seen_at, last_seen_at, symbols)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
                    (article["id"], article["source"], article["url"], article["title"],
                     article["published_at_epoch"], observed_at, observed_at,
                     json.dumps(article["symbols"], ensure_ascii=False)))

    def news_articles(self, limit=30):
        rows = self.db.execute("""SELECT id, source, url, title, published_at,
            first_seen_at, last_seen_at, symbols FROM news_articles
            ORDER BY first_seen_at DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(id=r[0], source=r[1], url=r[2], title=r[3],
                     published_at=r[4], first_seen_at=r[5], last_seen_at=r[6],
                     symbols=json.loads(r[7])) for r in rows]

    def close(self):
        self.db.close()


class ProcessLock:
    """OS lock released even after a crash; never delete a live lock file."""
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        if self.file is not None:
            raise RuntimeError("Este bloqueo ya esta adquirido")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise RuntimeError("Ya existe un proceso del bot para este estado") from None
        return self

    def __exit__(self, *_):
        if self.file is not None:
            self.file.close()
            self.file = None

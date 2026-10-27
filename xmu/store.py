import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from cryptography.fernet import Fernet

DEFAULTS = {'enabled': False, 'auto_number': True, 'auto_radar': True,
            'poll_interval': 30, 'history_interval': 21600,
            'wait_for_count': 10,
            'radar_mode': 'adaptive', 'latitude': None, 'longitude': None}


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = self.directory/'secret.key'
        try:
            fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(Fernet.generate_key())
        except FileExistsError:
            pass
        self.cipher = Fernet(key.read_bytes())
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.directory/'attendance.sqlite3', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS courses(
                owner TEXT, id TEXT, name TEXT, semester TEXT DEFAULT '',
                sync_complete INTEGER DEFAULT 0, sync_error TEXT DEFAULT '', synced_at REAL,
                PRIMARY KEY(owner,id));
            CREATE TABLE IF NOT EXISTS records(
                owner TEXT, id TEXT, course_id TEXT, kind TEXT, status TEXT,
                occurred_at TEXT, updated_at REAL, PRIMARY KEY(owner,id));
            CREATE TABLE IF NOT EXISTS attempts(
                owner TEXT,id TEXT,outcome TEXT,created_at REAL,updated_at REAL,
                PRIMARY KEY(owner,id));
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,owner TEXT,created_at REAL,
                level TEXT,message TEXT);
        ''')
        self.db.commit()
        if not self.get('device_id'):
            self.put('device_id', str(uuid.uuid4()))

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.lock, self.db:
            self.db.execute('INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                            (key, json.dumps(value, ensure_ascii=False)))

    @property
    def owner(self):
        return str(self.get('profile', {}).get('id', ''))

    def secret(self):
        token = self.get('secret')
        return json.loads(self.cipher.decrypt(token.encode())) if token else {}

    def set_secret(self, value):
        self.put('secret', self.cipher.encrypt(json.dumps(value).encode()).decode())

    def set_profile(self, value):
        self.put('profile', value)

    def settings(self):
        return DEFAULTS | self.get('settings', {})

    def update_settings(self, changes):
        settings = self.settings() | {k:v for k,v in changes.items() if k in DEFAULTS}
        self.put('settings', settings)
        return settings

    def save_course(self, cid, name, semester=''):
        with self.lock, self.db:
            self.db.execute('''INSERT INTO courses(owner,id,name,semester) VALUES(?,?,?,?)
                ON CONFLICT(owner,id) DO UPDATE SET name=excluded.name,
                semester=CASE WHEN excluded.semester='' THEN courses.semester ELSE excluded.semester END''',
                (self.owner, str(cid), name, semester))

    def sync_state(self, cid, complete, error=''):
        with self.lock, self.db:
            self.db.execute('UPDATE courses SET sync_complete=?,sync_error=?,synced_at=? WHERE owner=? AND id=?',
                            (int(complete), error, time.time(), self.owner, str(cid)))

    def save_record(self, rid, cid, kind, status, occurred_at=''):
        with self.lock, self.db:
            self.db.execute('''INSERT INTO records VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(owner,id) DO UPDATE SET course_id=excluded.course_id,kind=excluded.kind,
                status=CASE WHEN excluded.status='unknown' AND records.status NOT IN ('unknown','pending')
                       THEN records.status ELSE excluded.status END,
                occurred_at=CASE WHEN excluded.occurred_at='' THEN records.occurred_at ELSE excluded.occurred_at END,
                updated_at=excluded.updated_at''', (self.owner,str(rid),str(cid),kind,status,occurred_at,time.time()))

    def records(self, cid):
        with self.lock:
            return [dict(r) for r in self.db.execute('''SELECT records.*, attempts.outcome AS attempt_outcome
                FROM records LEFT JOIN attempts ON attempts.owner=records.owner AND attempts.id=records.id
                WHERE records.owner=? AND course_id=? ORDER BY occurred_at DESC,id DESC''', (self.owner,str(cid)))]

    def courses(self):
        with self.lock:
            return [dict(r) for r in self.db.execute('''SELECT c.*,COUNT(r.id) AS total,
                COALESCE(SUM(r.status='present'),0) AS present,
                COALESCE(SUM(r.status='absent'),0) AS absent,
                COALESCE(SUM(r.status='leave'),0) AS leave,
                COALESCE(SUM(r.status='late'),0) AS late,
                COALESCE(SUM(r.status='unknown'),0) AS unknown,
                COALESCE(SUM(r.status='pending'),0) AS pending
                FROM courses c LEFT JOIN records r ON r.owner=c.owner AND r.course_id=c.id
                WHERE c.owner=? GROUP BY c.id ORDER BY absent DESC,c.name''', (self.owner,))]

    def claim(self, rid):
        with self.lock, self.db:
            cur = self.db.execute('INSERT OR IGNORE INTO attempts VALUES(?,?,?,?,?)',
                                  (self.owner,str(rid),'submitting',time.time(),time.time()))
            return cur.rowcount == 1

    def finish(self, rid, outcome):
        with self.lock, self.db:
            self.db.execute('UPDATE attempts SET outcome=?,updated_at=? WHERE owner=? AND id=?',
                            (outcome,time.time(),self.owner,str(rid)))

    def attempt(self, rid):
        with self.lock:
            row = self.db.execute('SELECT * FROM attempts WHERE owner=? AND id=?', (self.owner,str(rid))).fetchone()
            return dict(row) if row else None

    def log(self, message, level='info'):
        with self.lock, self.db:
            self.db.execute('INSERT INTO events(owner,created_at,level,message) VALUES(?,?,?,?)',
                            (self.owner,time.time(),level,message))
            self.db.execute('DELETE FROM events WHERE id < (SELECT COALESCE(MAX(id),0)-2000 FROM events)')

    def logs(self):
        with self.lock:
            return [dict(r) for r in self.db.execute('SELECT created_at,level,message FROM events WHERE owner=? ORDER BY id DESC LIMIT 80', (self.owner,))]

"""Service-only decoded input archive for thirty-day download-cache retention."""
from __future__ import annotations
import time

from relay.institution_directory import InstitutionDirectory as BaseInstitutionDirectory
from relay.protocol import ProtocolError, canonical


class InstitutionDirectory(BaseInstitutionDirectory):
    def __init__(self, path, keys, *, clock=time.time):
        super().__init__(path,keys,clock=clock)
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS ip_input_data(seq INTEGER PRIMARY KEY,body TEXT NOT NULL)")

    def _input(self, seq, path, blob):
        super()._input(seq,path,blob)
        value=self._decode(blob)
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO ip_input_data VALUES(?,?)",(seq,canonical(value).decode()))

    def archive_old_inputs(self, *, limit=50):
        """Backfill parsed data before expiry; never replay old directory snapshots."""
        if type(limit) is not int or not 1<=limit<=100:raise ValueError("DIRECTORY_ARCHIVE_LIMIT_INVALID")
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS ip_input_backfill(singleton INTEGER PRIMARY KEY,position INTEGER,ceiling INTEGER)")
            if "ceiling" not in {r[1] for r in db.execute("PRAGMA table_info(ip_input_backfill)")}:db.execute("ALTER TABLE ip_input_backfill ADD COLUMN ceiling INTEGER")
            cursor=db.execute("SELECT position,ceiling FROM ip_input_backfill WHERE singleton=1").fetchone()
            position=cursor[0] if cursor else 0;ceiling=cursor[1] if cursor else None
            if ceiling is None or position>=ceiling:
                position=0;ceiling=db.execute("SELECT COALESCE(MAX(seq),0) FROM ip_input").fetchone()[0]
            query="SELECT seq,blob FROM ip_input WHERE state='APPLIED' AND blob IS NOT NULL AND seq>? AND seq<=? AND NOT EXISTS (SELECT 1 FROM ip_input_data d WHERE d.seq=ip_input.seq) ORDER BY seq LIMIT ?"
            rows=db.execute(query,(position,ceiling,limit)).fetchall()
            if not rows and position:
                ceiling=db.execute("SELECT COALESCE(MAX(seq),0) FROM ip_input").fetchone()[0]
                rows=db.execute(query,(0,ceiling,limit)).fetchall()
            if rows:db.execute("INSERT INTO ip_input_backfill VALUES(1,?,?) ON CONFLICT(singleton) DO UPDATE SET position=excluded.position,ceiling=excluded.ceiling",(rows[-1]['seq'],ceiling))
        stored=0
        for row in rows:
            try:value=self._decode(bytes(row["blob"]))
            except (ProtocolError,ValueError,TypeError):continue
            with self.db() as db:
                db.execute("INSERT OR IGNORE INTO ip_input_data VALUES(?,?)",(row["seq"],canonical(value).decode()))
            stored+=1
        return stored

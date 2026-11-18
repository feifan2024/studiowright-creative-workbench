"""Move only known automatic music keywords into music categories."""
from contextlib import closing
import sqlite3
from music_metadata import grouped_tags

def migrate(path,apply=False):
    with closing(sqlite3.connect(path,timeout=2)) as db, db:
        if apply:db.execute('BEGIN IMMEDIATE')
        rows=db.execute("SELECT d.asset_id,d.value FROM dimension_tags d JOIN assets a ON a.id=d.asset_id WHERE a.media_type='audio' AND d.dimension='keywords' AND d.source='music_prompt'").fetchall()
        result={'assets':0,'moved':0,'manual_skipped':0}
        seen=set()
        for ident,value in rows:
            groups=grouped_tags([value])
            targets=[dim for dim,values in groups.items() if values]
            if not targets:continue
            if db.execute("SELECT 1 FROM manual_dimensions WHERE asset_id=? AND dimension IN ('keywords',?)",(ident,targets[0])).fetchone():
                result['manual_skipped']+=1;continue
            seen.add(ident);result['moved']+=1
            if apply:
                db.execute("INSERT OR IGNORE INTO dimension_tags VALUES(?,?,?,'music_prompt')",(ident,targets[0],value))
                db.execute("DELETE FROM dimension_tags WHERE asset_id=? AND dimension='keywords' AND value=? AND source='music_prompt'",(ident,value))
        result['assets']=len(seen)
        return result

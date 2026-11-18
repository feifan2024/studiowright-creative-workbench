from contextlib import closing
"""Append prompt tags to historical audio; no generation, files or origins changed."""
import argparse
import json
from pathlib import Path
import sqlite3
import time
from music_metadata import prompt_tags


def candidates(db):
    rows=db.execute("SELECT o.asset_id,o.source_id,o.prompt,o.details FROM origins o JOIN assets a ON a.id=o.asset_id WHERE o.source='flow_music' AND a.media_type='audio' ORDER BY o.asset_id,o.source_id").fetchall()
    grouped={}
    for ident,source_id,prompt,raw in rows:
        details=json.loads(raw or '{}')
        text=details.get('generation_prompt')
        source='generation_prompt'
        if not isinstance(text,str):
            # The legacy importer stored title + fullwidth colon + prompt.
            text=(prompt or '').split('：',1)[-1]
            source='legacy_prompt_after_title' if '：' in (prompt or '') else 'origin_prompt'
        if not text.strip():continue
        grouped.setdefault(ident,[]).append((source_id,source,prompt_tags(text)))
    return grouped


def backfill(path,apply=False):
    with closing(sqlite3.connect(str(path),timeout=2)) as db, db:
        # A short write transaction rechecks manual edits; never touch task databases.
        if apply:db.execute('BEGIN IMMEDIATE')
        groups=candidates(db);result={'audio_assets':len(groups),'updated_assets':0,'added_tags':0,'manual_skipped':0,'items':[]}
        for ident,origins in groups.items():
            if db.execute("SELECT 1 FROM manual_dimensions WHERE asset_id=? AND dimension='keywords'",(ident,)).fetchone():
                result['manual_skipped']+=1;continue
            existing={r[0] for r in db.execute("SELECT value FROM dimension_tags WHERE asset_id=? ",(ident,))}
            wanted=list(dict.fromkeys(t for _,_,tags in origins for t in tags))
            missing=[t for t in wanted if t not in existing]
            if not missing:continue
            result['updated_assets']+=1;result['added_tags']+=len(missing)
            result['items'].append({'asset_id':ident,'added_tags':missing})
            if apply:
                db.execute('CREATE TABLE IF NOT EXISTS music_prompt_backfill(asset_id TEXT,source_id TEXT,prompt_source TEXT,tags TEXT,created REAL,PRIMARY KEY(asset_id,source_id))')
                db.executemany("INSERT OR IGNORE INTO dimension_tags VALUES(?,'keywords',?,'music_prompt')",[(ident,t) for t in missing])
                for source_id,source,tags in origins:
                    db.execute('INSERT OR IGNORE INTO music_prompt_backfill VALUES(?,?,?,?,?)',(ident,source_id,source,json.dumps(tags,ensure_ascii=False),time.time()))
        return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('database',type=Path);parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    if not args.database.is_file():parser.error('素材数据库不存在')
    print(json.dumps(backfill(args.database,args.apply),ensure_ascii=False,indent=2))

"""Consistent per-SQLite backups under the runtime lock; stop Bot before use."""
import argparse
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path
import shutil
import sqlite3
from system_data import readonly,encode
from system_runtime import read_config,process_lock


def backup(config,folder):
    c=read_config(config)
    folder=Path(folder).resolve()
    if folder.exists():raise ValueError('Backup folder must be new')
    with process_lock(c['paths']['runtime']+'.lock'):
        folder.mkdir(parents=True)
        shutil.copyfile(config,folder/'paper.json')
        mapping={}
        paths=dict(c['paths'])
        paths['action_observations']=str(Path(c['paths']['actions']).with_suffix('.observations.sqlite3'))
        budget=Path(c['budget_file'])
        if budget.exists():
            import json
            paths['budget_config']=str(budget)
            settings=json.loads(budget.read_text(encoding='utf-8-sig'))
            if settings.get('ledger'):paths['budget_ledger']=str((budget.parent/settings['ledger']).resolve())
        for name,raw in paths.items():
            path=Path(raw)
            if not path.exists():continue
            target=folder/(name+path.suffix)
            if path.suffix in ('.db','.sqlite3'):
                with closing(readonly(path)) as source,closing(sqlite3.connect(target)) as dest:
                    source.backup(dest)
                    if dest.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Backup integrity check failed')
            else:shutil.copyfile(path,target)
            mapping[name]=dict(source=str(path),backup=str(target))
        (folder/'manifest.json').write_text(encode(mapping),encoding='utf-8')
    return folder


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',default='config/paper.json')
    p.add_argument('--output',type=Path,default=Path('data/backups')/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f'))
    args=p.parse_args()
    print(backup(args.config,args.output))

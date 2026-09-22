"""利用可能な本文テキストを出典付きで登録。URLの自動取得はしません。"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit
from news_collector import ROOT

DEFAULT_DB = ROOT / 'data/news_documents.sqlite3'
MAX_CHARS = 12000


def register(path, text, title, url, symbols, published_at=None, kind='excerpt', metadata=None):
    if not text.strip() or len(text) > MAX_CHARS:
        raise ValueError('本文は1〜12000文字。長い資料は必要箇所を抜粋してください')
    if not title.strip() or len(title) > 2000 or kind not in ('full', 'excerpt'):
        raise ValueError('タイトルまたは本文種別が不正')
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('出典には認証情報を含まないHTTP(S) URLを指定してください')
    if not symbols or any(len(s) != 4 or not s.isascii() or not s.isalnum() for s in symbols):
        raise ValueError('銘柄コードは4文字で指定してください')
    if published_at:
        stamp = datetime.fromisoformat(published_at)
        if stamp.tzinfo is None:
            raise ValueError('公表日時には +09:00 などのタイムゾーンが必要です')
        published_at = stamp.astimezone(timezone.utc).isoformat()
    article = dict(id=hashlib.sha256(url.encode()).hexdigest(), title=title, url=url,
                   symbols=sorted(set(symbols)), published_at=published_at, publisher='',
                   body=text, body_status=kind, source_method='local_text_user_supplied')
    if metadata:
        allowed = {'source_method', 'publisher', 'published_date', 'date_quality', 'parser_version'}
        if set(metadata) - allowed:
            raise ValueError('未対応の本文メタデータ')
        article.update(metadata)
    revision = hashlib.sha256(json.dumps(article, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    received = datetime.now(timezone.utc).isoformat()
    article.update(first_seen_at=received, observed_at=received, body_received_at=received,
                   document_revision=revision)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS documents (revision TEXT PRIMARY KEY, article_id TEXT, received_at TEXT, payload TEXT)')
        inserted = db.execute('INSERT OR IGNORE INTO documents VALUES (?,?,?,?)',
                             (revision, article['id'], received, json.dumps(article, ensure_ascii=False))).rowcount
    return revision, bool(inserted)


def snapshot(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        rows = db.execute('SELECT article_id,payload FROM documents ORDER BY received_at DESC,rowid DESC').fetchall()
    result, seen = [], set()
    for ident, payload in rows:
        if ident not in seen:
            result.append(json.loads(payload))
            seen.add(ident)
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['import', 'list'])
    p.add_argument('--db', type=Path, default=DEFAULT_DB)
    p.add_argument('--text', type=Path)
    p.add_argument('--title')
    p.add_argument('--url')
    p.add_argument('--symbols', nargs='+')
    p.add_argument('--published-at')
    p.add_argument('--kind', choices=['full', 'excerpt'], default='excerpt')
    a = p.parse_args(argv)
    try:
        if a.command == 'list':
            for row in snapshot(a.db):
                print(row['id'][:12], row['body_status'], row['title'], row['body_received_at'])
                if row.get('source_method') == 'official_html':
                    print('出典:', row['url'])
                    print('公表日:', row.get('published_date') or '不明', '/ 公表日時:', row.get('published_at') or '不明', '/ 日付品質:', row.get('date_quality'))
            return 0
        if not all((a.text, a.title, a.url, a.symbols)):
            raise ValueError('--text --title --url --symbols を指定してください')
        if a.text.stat().st_size > MAX_CHARS * 4:
            raise ValueError('テキストファイルが大きすぎます')
        revision, inserted = register(a.db, a.text.read_text(encoding='utf-8-sig'), a.title,
                                     a.url, a.symbols, a.published_at, a.kind)
        print('登録成功' if inserted else '登録済み（取得日時は変更しません）', revision[:12])
        print('API通信なし。本文の送信は news_ai.py run --body を実行したときだけです。')
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print('停止:', exc)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

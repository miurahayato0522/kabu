"""RSS見出しの収集・保存。本文取得、AI呼び出し、実注文は行わない。"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / 'data' / 'news.sqlite3'
MAX_BYTES = 2_000_000


def now():
    return datetime.now(timezone.utc).isoformat()


def feed_url(name, exact=True):
    return 'https://news.google.com/rss/search?' + urllib.parse.urlencode(
        {'q': (f'"{name}"' if exact else f'({name})')+' when:7d', 'hl': 'ja', 'gl': 'JP', 'ceid': 'JP:ja'})


def fetch(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'StockNewsCollector/0.1'})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = response.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        raise ValueError('RSSのサイズ上限超過')
    return payload


def parse_feed(payload):
    if len(payload) > MAX_BYTES or b'<!DOCTYPE' in payload.upper() or b'<!ENTITY' in payload.upper():
        raise ValueError('RSSのサイズまたはXML宣言が不正')
    root = ET.fromstring(payload)
    if root.tag != 'rss' or root.find('channel') is None:
        raise ValueError('RSS形式ではありません')
    articles, rejected = [], 0
    for item in root.findall('./channel/item'):
        title = (item.findtext('title') or '').strip()
        url = (item.findtext('link') or '').strip()
        parsed = urllib.parse.urlsplit(url)
        if not title or parsed.scheme not in ('http', 'https') or not parsed.hostname:
            rejected += 1
            continue
        raw_date = item.findtext('pubDate') or ''
        published, quality = None, 'missing'
        if raw_date:
            try:
                stamp = parsedate_to_datetime(raw_date)
                if stamp.tzinfo is None:
                    raise ValueError('timezone missing')
                published, quality = stamp.astimezone(timezone.utc).isoformat(), 'ok'
            except (TypeError, ValueError, OverflowError):
                quality = 'invalid'
        # 同一URLは銘柄検索をまたいで1記事。同じ事件の別URLまでは統合しない。
        key = hashlib.sha256(url.encode()).hexdigest()
        articles.append({'id': key, 'title': title, 'url': url,
                         'publisher': item.findtext('source') or '',
                         'published_at': published, 'published_raw': raw_date,
                         'date_quality': quality, 'guid': item.findtext('guid') or ''})
    return articles, rejected


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS articles (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL UNIQUE,
            publisher TEXT, published_at TEXT, published_raw TEXT, date_quality TEXT,
            guid TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
            body_status TEXT NOT NULL DEFAULT 'not_fetched');
        CREATE TABLE IF NOT EXISTS matches (
            article_id TEXT REFERENCES articles(id), symbol TEXT, query TEXT,
            first_seen_at TEXT, PRIMARY KEY(article_id, symbol, query));
        CREATE TABLE IF NOT EXISTS observations (
            article_id TEXT REFERENCES articles(id), observed_at TEXT,
            title TEXT, published_at TEXT, published_raw TEXT, date_quality TEXT,
            publisher TEXT, guid TEXT,
            UNIQUE(article_id, title, published_raw, publisher, guid));
        CREATE TABLE IF NOT EXISTS fetch_runs (
            id INTEGER PRIMARY KEY, symbol TEXT, feed_url TEXT,
            started_at TEXT, finished_at TEXT, status TEXT,
            received INTEGER, inserted INTEGER, rejected INTEGER, error TEXT);
    ''')
    return db


def save(db, articles, symbol, query, observed):
    inserted = 0
    for a in articles:
        cursor = db.execute('''INSERT OR IGNORE INTO articles
            (id,title,url,publisher,published_at,published_raw,date_quality,guid,first_seen_at,last_seen_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)''',
            tuple(a[k] for k in ('id','title','url','publisher','published_at','published_raw','date_quality','guid'))
            + (observed, observed))
        inserted += cursor.rowcount
        # 初回内容を上書きせず、変更された見出し等は別の観測履歴に保存。
        db.execute('UPDATE articles SET last_seen_at=? WHERE id=?', (observed, a['id']))
        db.execute('INSERT OR IGNORE INTO matches VALUES (?,?,?,?)', (a['id'], symbol, query, observed))
        db.execute('INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?)',
                   (a['id'], observed, a['title'], a['published_at'], a['published_raw'],
                    a['date_quality'], a['publisher'], a['guid']))
    return inserted


def collect(path, symbols, fetcher=fetch, sleep=time.sleep):
    failures = 0
    with closing(connect(path)) as db:
        for index, (symbol, name) in enumerate(symbols.items()):
            if index:
                sleep(2)
            started, url = now(), feed_url(name, exact=not symbol.startswith('@'))
            try:
                articles, rejected = parse_feed(fetcher(url))
                if rejected:
                    raise ValueError('RSSに不正な項目があります。完全な空結果として扱いません')
                observed = now()
                with db:
                    inserted = save(db, articles, symbol, name, observed)
                    db.execute('''INSERT INTO fetch_runs
                        (symbol,feed_url,started_at,finished_at,status,received,inserted,rejected,error)
                        VALUES (?,?,?,?,?,?,?,?,?)''',
                        (symbol,url,started,observed,'ok',len(articles),inserted,rejected,None))
                print(f'{symbol} {name}: 受信{len(articles)}件 / 新規{inserted}件 / 不正項目{rejected}件', flush=True)
            except (OSError, ValueError, ET.ParseError) as exc:
                failures += 1
                with db:
                    db.execute('''INSERT INTO fetch_runs
                        (symbol,feed_url,started_at,finished_at,status,error) VALUES (?,?,?,?,?,?)''',
                        (symbol,url,started,now(),'failed',type(exc).__name__))
                print(f'{symbol}: 取得失敗 {type(exc).__name__}（次の銘柄へ続行）', flush=True)
    return failures


def read_news(path, symbol=None, limit=20):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('''SELECT a.*, (SELECT group_concat(DISTINCT symbol) FROM matches
            WHERE article_id=a.id) AS symbols FROM articles a
            WHERE (? IS NULL OR EXISTS (SELECT 1 FROM matches WHERE article_id=a.id AND symbol=?))
            ORDER BY first_seen_at DESC, id LIMIT ?''', (symbol, symbol, limit)).fetchall()
        return [dict(row) for row in rows]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['fetch', 'list', 'status'])
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    parser.add_argument('--config', type=Path, default=ROOT / 'news_sources.json')
    parser.add_argument('--symbol', help='4桁の銘柄コード（省略時は全銘柄）')
    parser.add_argument('--limit', type=int, default=20)
    args = parser.parse_args(argv)
    try:
        if args.limit < 1:
            raise ValueError('limitは1以上を指定してください')
        if args.command == 'fetch':
            symbols = json.loads(args.config.read_text(encoding='utf-8-sig'))['symbols']
            if not isinstance(symbols, dict) or not symbols or any(
                    not isinstance(name, str) or not name.strip() for name in symbols.values()):
                raise ValueError('銘柄設定が不正です')
            if args.symbol:
                symbols = {args.symbol: symbols[args.symbol]}
            print('RSS見出し取得 / 本文未取得 / AI・注文なし', flush=True)
            failures = collect(args.db, symbols)
            print(f'保存先: {args.db.resolve()}')
            return 1 if failures else 0
        if args.command == 'list':
            for row in read_news(args.db, args.symbol, args.limit):
                print(f"\n[{row['symbols']}] {row['title']}\n配信元: {row['publisher']} / 本文未取得")
                print(f"公開: {row['published_at'] or '不明'} / 初回取得: {row['first_seen_at']}\n{row['url']}")
            print('\n時刻はUTC。銘柄との関連は検索候補であり、確定判定ではありません。')
        else:
            with closing(sqlite3.connect(args.db.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                for table in ('articles', 'matches', 'observations', 'fetch_runs'):
                    print(f'{table}: {db.execute("SELECT count(*) FROM " + table).fetchone()[0]}件')
                for row in db.execute('SELECT symbol,finished_at,status,received,inserted,error FROM fetch_runs ORDER BY id DESC LIMIT 10'):
                    print(row)
        return 0
    except (OSError, sqlite3.Error, ValueError, KeyError) as exc:
        print(f'停止: {exc}')
        return 1
    except KeyboardInterrupt:
        print('\n中止しました。完了済みの取得結果は保存されています。')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

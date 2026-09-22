"""公式PDFをページ指定で保存・本文登録。AI送信や注文はしない。"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import re
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from pypdf import PdfReader

from news_documents import DEFAULT_DB, MAX_CHARS, register

ROOT = Path(__file__).resolve().parent
MAX_BYTES = 15 * 1024 * 1024
MAX_PAGES = 200
PARSER = 'official-pdf-pages-v1'
ALLOWED_HOSTS = {'global.toyota', 'www.nissan-global.com'}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in ALLOWED_HOSTS or parsed.username
            or parsed.password or parsed.query or parsed.fragment or not parsed.path.lower().endswith('.pdf')):
        raise ValueError('対応URLはトヨタまたは日産の公式HTTPS PDFのみです。クエリ・リダイレクト・他社PDFは未対応です')
    return url


def download(url):
    validate_url(url)
    request = urllib.request.Request(url, headers={'User-Agent': 'KabuResearchCollector/0.1', 'Accept': 'application/pdf'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=45) as response:
            if response.headers.get_content_type() != 'application/pdf':
                raise ValueError('PDF以外の応答です。保存しません')
            data = response.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError('PDFが15MBを超えています。保存しません')
            return data
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise ValueError(f'取得失敗 HTTP {status}。リダイレクト回避や自動再試行はしません') from None
    except urllib.error.URLError:
        raise ValueError('通信失敗。接続状況を確認してください。自動再試行はしません') from None


def page_numbers(value, total):
    try:
        pages = [int(item) for item in value.split(',')]
    except ValueError:
        raise ValueError('ページ番号は 12 または 11,12 の形式で指定してください') from None
    if not pages or len(pages) != len(set(pages)) or any(page < 1 or page > total for page in pages):
        raise ValueError(f'ページ番号は1〜{total}の重複なしで指定してください')
    return pages


def extract(data, pages=None):
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted and reader.decrypt('') == 0:
            raise ValueError('パスワード保護PDFは未対応です')
        if not 1 <= len(reader.pages) <= MAX_PAGES:
            raise ValueError('PDFページ数が範囲外です')
        selected = page_numbers(pages, len(reader.pages)) if pages else list(range(1, len(reader.pages) + 1))
        text = []
        for page in selected:
            content = reader.pages[page - 1].extract_text(extraction_mode='layout') or ''
            if not content.strip():
                raise ValueError(f'{page}ページはテキスト抽出できません。画像PDF・表崩れは未対応です')
            text.append((page, content.strip()))
    except (ValueError, OSError) as exc:
        raise ValueError(str(exc)) from None
    return len(reader.pages), text


def date_from_first_pages(data):
    probe = PdfReader(BytesIO(data))
    if probe.is_encrypted and probe.decrypt('') == 0:
        return None
    if not probe.pages:
        return None
    requested = ','.join(map(str, range(1, min(2, len(probe.pages)) + 1)))
    _, pages = extract(data, requested)
    text = '\n'.join(value for _, value in pages)
    matches = re.findall(r'(?<!\d)(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', text)
    if len(set(matches)) != 1:
        return None
    return datetime(*map(int, matches[0])).date().isoformat()


def archive_fetch(url, docs, archive, symbol, title, pages, fetcher=download):
    validate_url(url)
    Path(archive).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(archive)) as database:
        database.execute('''CREATE TABLE IF NOT EXISTS fetches (
            id INTEGER PRIMARY KEY,url TEXT,started_at TEXT,received_at TEXT,status TEXT,error TEXT,
            pdf BLOB,sha256 TEXT,page_count INTEGER,selected_pages TEXT,document_revision TEXT)''')
        with database:
            ident = database.execute('INSERT INTO fetches (url,started_at,status) VALUES (?,?,?)',
                                     (url, datetime.now(timezone.utc).isoformat(), 'started')).lastrowid
        try:
            data = fetcher(url)
            if not isinstance(data, bytes) or not data.startswith(b'%PDF-') or len(data) > MAX_BYTES:
                raise ValueError('PDFの形式またはサイズが不正')
            total, extracted = extract(data, pages)
            body = '\n\n'.join(f'[PDF {page}ページ]\n{text}' for page, text in extracted)
            if len(body) > MAX_CHARS:
                raise ValueError(f'選択した本文が{MAX_CHARS}文字を超えます。ページ数を絞ってください')
            published_date = date_from_first_pages(data)
            metadata = {'source_method': 'official_pdf_pages', 'publisher': urlsplit(url).hostname,
                        'parser_version': PARSER, 'published_date': published_date,
                        'date_quality': 'date_only' if published_date else 'unknown'}
            revision, inserted = register(docs, body, title, url, [symbol], None, 'excerpt', metadata)
            with database:
                database.execute('''UPDATE fetches SET received_at=?,status=?,pdf=?,sha256=?,page_count=?,
                    selected_pages=?,document_revision=? WHERE id=?''',
                    (datetime.now(timezone.utc).isoformat(), 'ok', data, hashlib.sha256(data).hexdigest(),
                     total, pages, revision, ident))
            return {'title': title, 'pages': [page for page, _ in extracted], 'characters': len(body),
                    'published_date': published_date, 'revision': revision, 'inserted': inserted}
        except (OSError, ValueError, sqlite3.Error) as exc:
            with database:
                database.execute('UPDATE fetches SET status=?,error=? WHERE id=?', ('failed', str(exc), ident))
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['preview', 'import'])
    parser.add_argument('url')
    parser.add_argument('--pages', help='PDF内の1始まりページ番号。例: 12 または 11,12')
    parser.add_argument('--symbol', choices=['7203', '7201'])
    parser.add_argument('--title')
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    parser.add_argument('--archive', type=Path, default=ROOT / 'data/news_pdf_fetches.sqlite3')
    args = parser.parse_args(argv)
    try:
        if args.command == 'preview':
            data = download(args.url)
            total, extracted = extract(data, args.pages)
            print('PDF:', total, 'ページ / 公表日候補:', date_from_first_pages(data) or '不明')
            for page, content in extracted:
                print(f'\n--- {page}ページ ---\n{content[:1600]}')
            print('\n登録・AI送信・注文なし。ページ番号と記事タイトルを確認して import を実行してください。')
            return 0
        if not args.pages or not args.symbol or not args.title:
            raise ValueError('import には --pages --symbol --title を指定してください')
        result = archive_fetch(args.url, args.db, args.archive, args.symbol, args.title, args.pages)
        print('登録成功' if result['inserted'] else '登録済み（取得履歴のみ追加）', result['title'])
        print('対象ページ:', ','.join(map(str, result['pages'])), '/ 本文:', result['characters'], '文字 / 公表日:', result['published_date'] or '不明')
        print('公表日時: 時刻を確認できません。本文は保存しますがニュース条件は見送りです')
        print('本文DB:', args.db.resolve())
        print('取得履歴:', args.archive.resolve())
        print('AI送信・課金・注文なし。PDF全体やリンク先は分析対象にしません。')
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print('停止:', exc)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

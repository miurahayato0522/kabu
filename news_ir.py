"""対応する企業公式HTMLを1件取得して登録。PDF・リンク先・AIは自動実行しない。"""
import argparse
from contextlib import closing
from datetime import datetime, timezone, timedelta
import hashlib
from pathlib import Path
import re
import sqlite3
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from news_documents import register, DEFAULT_DB, MAX_CHARS

ROOT=Path(__file__).resolve().parent
MAX_BYTES=3*1024*1024
PARSER='toyota-corporate-html-v1'


def supported_url(url):
    p=urlsplit(url)
    if (p.scheme!='https' or p.netloc!='global.toyota' or p.query or p.fragment
        or not re.fullmatch(r'/jp/newsroom/corporate/[0-9]+\.html',p.path)):
        raise ValueError('対応URLは https://global.toyota/jp/newsroom/corporate/数字.html のみです。PDFや他社・他ページ形式は未対応です')
    return url


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        return None


def download(url):
    supported_url(url)
    req=urllib.request.Request(url,headers={'User-Agent':'KabuResearchCollector/0.1','Accept':'text/html'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req,timeout=30) as response:
            if response.headers.get_content_type()!='text/html':
                raise ValueError('HTML以外の応答です。登録しません')
            raw=response.read(MAX_BYTES+1)
            if len(raw)>MAX_BYTES:
                raise ValueError('取得サイズが3MBを超えています。登録しません')
            return raw
    except urllib.error.HTTPError as exc:
        code=exc.code;exc.close()
        raise ValueError(f'取得失敗 HTTP {code}。リダイレクト・制限の回避や自動再試行はしません') from None
    except urllib.error.URLError:
        raise ValueError('通信失敗。接続状況を確認してください。自動再試行はしません') from None


def parse(raw):
    soup=BeautifulSoup(raw.decode('utf-8-sig',errors='strict'),'html.parser')
    main=soup.select('.contents_main')
    if len(main)!=1:
        raise ValueError('本文領域が特定できません。ページ形式の変更または未対応です')
    main=main[0]
    titles=main.select('.article_info h1.title')
    if len(titles)!=1 or not titles[0].get_text(strip=True):
        raise ValueError('記事タイトルを特定できません')
    title=titles[0].get_text(' ',strip=True)
    dates=main.select('.article_info .date')
    published_date=None
    if len(dates)==1:
        m=re.fullmatch(r'(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日',dates[0].get_text(strip=True))
        if m:
            published_date=datetime(*map(int,m.groups())).date().isoformat()
    # 記事専用の公開時刻メタデータだけを読む。開催時刻・更新時刻は公表時刻にしない。
    times=[t.get('content','').strip() for t in soup.select('meta[property="article:published_time"]')]
    published_at=None
    if len(times)==1 and re.search(r'T\d{2}:\d{2}',times[0]):
        t=datetime.fromisoformat(times[0].replace('Z','+00:00'))
        if t.tzinfo is not None and (not published_date or t.astimezone(timezone(timedelta(hours=9))).date().isoformat()==published_date):
            published_at=t.astimezone(timezone.utc).isoformat()
    chunks=[]
    for section in main.find_all('div',recursive=False):
        classes=section.get('class',[])
        if 'section' not in classes or 'html' not in classes or 'article_info' in classes:
            continue
        for tag in section.select('script,style,iframe,form,nav,button,noscript'):
            tag.decompose()
        heading=section.find(['h2','h3','h4'])
        if heading and heading.get_text(strip=True) in ('関連リンク','将来予測・インサイダー取引について'):
            continue
        text=section.get_text('\n',strip=True)
        if text=='以上':
            break
        if text:
            chunks.append(text)
    body='\n\n'.join(chunks)
    if not body or len(body)>MAX_CHARS:
        raise ValueError('本文が空、または12000文字超です。自動で切り詰めず登録を停止します')
    return dict(title=title,body=body,published_at=published_at,
                metadata=dict(publisher='トヨタ自動車',source_method='official_html',parser_version=PARSER,
                              published_date=published_date,date_quality='timestamp' if published_at else 'date_only' if published_date else 'unknown'))


def collect(url,docs,archive,fetcher=download):
    supported_url(url)
    Path(archive).parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(archive)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS fetches (id INTEGER PRIMARY KEY,url TEXT,started_at TEXT,received_at TEXT,status TEXT,error TEXT,raw_html BLOB,sha256 TEXT,document_revision TEXT)')
        with db:
            ident=db.execute('INSERT INTO fetches (url,started_at,status) VALUES (?,?,?)',
                             (url,datetime.now(timezone.utc).isoformat(),'started')).lastrowid
        try:
            raw=fetcher(url)
            if not isinstance(raw,bytes) or len(raw)>MAX_BYTES:
                raise ValueError('取得サイズまたは形式が不正')
            with db:
                db.execute('UPDATE fetches SET raw_html=?,sha256=?,received_at=?,status=? WHERE id=?',
                           (raw,hashlib.sha256(raw).hexdigest(),datetime.now(timezone.utc).isoformat(),'received',ident))
            article=parse(raw)
            revision,inserted=register(docs,article['body'],article['title'],url,['7203'],article['published_at'],
                                       kind='excerpt',metadata=article['metadata'])
            with db:
                db.execute('UPDATE fetches SET status=?,document_revision=? WHERE id=?',('ok',revision,ident))
            return article,inserted
        except (OSError,ValueError,sqlite3.Error) as exc:
            with db:
                db.execute('UPDATE fetches SET status=?,error=? WHERE id=?',('failed',str(exc),ident))
            raise


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('url')
    p.add_argument('--db',type=Path,default=DEFAULT_DB)
    p.add_argument('--archive',type=Path,default=ROOT/'data/news_ir_fetches.sqlite3')
    a=p.parse_args(argv)
    try:
        article,inserted=collect(a.url,a.db,a.archive)
        print('登録成功' if inserted else '登録済み（取得履歴のみ追加）',article['title'])
        print('銘柄: 7203 / 本文:',len(article['body']),'文字 / 公表日:',article['metadata']['published_date'] or '不明')
        print('公表日時:',article['published_at'] or '時刻を確認できません。本文は保存しますがニュース条件は見送りです')
        print('本文DB:',a.db.resolve())
        print('取得履歴:',a.archive.resolve())
        print('AI送信・課金・注文なし。リンク先の資料やPDFは取得していません。')
        return 0
    except (OSError,ValueError,sqlite3.Error) as exc:
        print('停止:',exc);return 1


if __name__=='__main__':raise SystemExit(main())

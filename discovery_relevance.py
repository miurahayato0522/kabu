"""Conservative preselection, not a forecast or entity-resolution oracle."""
import re
import unicodedata
from system_data import digest
PRODUCTS=('半導体製造装置','半導体テスト','イメージセンサー','自動車','医薬品','たばこ','通信ネットワーク','デジタルサービス','石油','天然ガス','オフィスビル','商業施設')
PATHS={'rare_earth':('採掘','精錬','精製','新規発見','輸出規制','供給'),
 'semiconductor':('工場新設','設備投資','製造装置','半導体不足','半導体需要','輸出規制'),
 'banking':('政策金利','利上げ','利下げ','金融政策'), 'real_estate':('政策金利','住宅ローン','利上げ','利下げ'),
 'export_manufacturing':('関税','輸出規制','円安','円高'), 'energy':('原油価格','石油価格','天然ガス価格','供給停止'), 'healthcare':('薬価','医薬品承認')}
LABELS={'company':'企業名の記載（法人照合候補）','product':'事業・製品の記載','supply_chain':'供給網・経済条件を通じた間接候補','industry':'同一産業のみ','unknown':'関連性未確認'}

def classify(article,company,relations,names):
    text=unicodedata.normalize('NFKC',article['title']+' '+article.get('body',''))
    ambiguous=any(re.search(re.escape(unicodedata.normalize('NFKC',name))+r'(?:データ|ドコモ|東日本|西日本|銀行|証券|モバイル|ソリューション|自動車九州)',text) for name in names)
    verified=[r for r in relations if r['state']=='確認済み']
    kind='unknown' if ambiguous else ('company' if names else 'industry');anchors=[]
    if not names:
        anchors=[p for p in PRODUCTS if p in text and any(p in r['business']+' '+r['quote'] for r in verified)]
        if anchors:kind='product'
        else:
            anchors=[w for r in verified for w in PATHS.get(r['topic'],()) if w in text]
            if anchors:kind='supply_chain'
    if not relations and not names:kind='unknown'
    eligible=bool(verified) and kind in ('company','product','supply_chain')
    reason='法人の同一性・親子関係を別途確認' if ambiguous else ('影響経路未確認・産業一致だけでは送信しない' if not eligible else '一次選別のみ。企業別解析と根拠確認が必要')
    return dict(kind=kind,label=LABELS[kind],anchors=sorted(set(anchors)),eligible=eligible,reason=reason,entity_ambiguous=ambiguous)

def event_key(article):
    title=article['title']
    if article.get('publisher') and title.endswith(' - '+article['publisher']):title=title[:-(len(article['publisher'])+3)]
    return digest([re.sub(r'\s+','',unicodedata.normalize('NFKC',article.get('body') or title)),(article.get('published_at') or '')[:10]])

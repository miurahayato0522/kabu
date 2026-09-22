"""Provider-neutral business-impact schema. No price forecasts or execution code."""
from typing import Protocol

DIRECTIONS = ['ポジティブ', 'ネガティブ', '中立', '不明']
IMPORTANCE = ['高', '中', '低', '不明']
CATEGORIES = ['金融政策','経済指標','為替・資源','産業動向']
PROMPT = '''\n企業業績・事業環境への好悪をLLMによる推定としてimpactsに記載してください。
候補銘柄ごとに1件。short_termとlong_termはポジティブ・ネガティブ・中立・不明。
市場ニュースは金融政策・経済指標・為替・資源・産業動向など適切なcategoryを選んでください。
短期は数日から数週、中長期は数月以上です。株価リターンの予測ではありません。
importanceは高・中・低・不明。報道件数の多さを重要度の根拠にしないでください。
根拠不足は不明。reasonは企業業績への意味、quoteは入力からの連続した原文引用。
市場予想との比較は入力に明記される場合だけexpectation_comparisonへ原文引用し、なければ不明。
間接関連、短期/中長期の違い、不確実性はunknownsへ記載してください。
取得していない本文、外部知識、市場予想、確率、予測株価、売買命令を生成しないでください。'''


class NewsAnalyzer(Protocol):
    def __call__(self, request: dict, key: str) -> dict:
        """Return a completed Responses-compatible envelope, validated by news_ai."""
        ...


def schema(codes):
    fields = {k: {'type':'string'} for k in ('symbol','short_term','long_term','importance','reason','quote','expectation_comparison')}
    if codes:
        fields['symbol']['enum'] = codes
    for k in ('short_term','long_term'):
        fields[k]['enum'] = DIRECTIONS
    fields['importance']['enum'] = IMPORTANCE
    return {'type':'array','minItems':len(codes),'maxItems':len(codes),'items':{
        'type':'object','additionalProperties':False,'properties':fields,'required':list(fields)}}


def validate(items, codes, source):
    if not isinstance(items,list) or len(items)!=len(codes):
        raise ValueError('好悪分類の候補銘柄数が不一致')
    seen=[]
    fields={'symbol','short_term','long_term','importance','reason','quote','expectation_comparison'}
    for item in items:
        if not isinstance(item,dict) or set(item)!=fields or any(not isinstance(v,str) or not v.strip() for v in item.values()):
            raise ValueError('好悪分類の形式が不正')
        if item['short_term'] not in DIRECTIONS or item['long_term'] not in DIRECTIONS or item['importance'] not in IMPORTANCE:
            raise ValueError('好悪分類の値が不正')
        if item['quote'] not in source:
            raise ValueError('好悪判断の原文根拠が不一致')
        if item['expectation_comparison']!='不明' and item['expectation_comparison'] not in source:
            raise ValueError('市場予想比較の原文根拠が不一致')
        seen.append(item['symbol'])
    if sorted(seen)!=sorted(codes):
        raise ValueError('好悪分類の候補銘柄が不一致')
    return items

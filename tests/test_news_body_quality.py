"""人工応答を使った検査ロジックのテスト。実モデルの抽出精度評価とは別。"""
import copy
import json
import unittest
from news_ai import parse_response, make_request, BODY_SCHEMA


class QualityTests(unittest.TestCase):
    def test_single_candidate_schema_limits_duplicates_without_mutating_global(self):
        article = dict(title='架空資料', body='トヨタの資料', body_status='full', symbols=['7203'])
        schema = make_request(article, {'7203': 'トヨタ自動車'})['text']['format']['schema']
        relations = schema['properties']['relations']
        self.assertEqual((relations['minItems'], relations['maxItems']), (1, 1))
        self.assertEqual(relations['items']['properties']['symbol']['enum'], ['7203'])
        self.assertNotIn('maxItems', BODY_SCHEMA['properties']['relations'])

    def result(self, body, numbers, reason='トヨタ自動車が予想を変更したと記載'):
        value = dict(summary='テスト資料', category='業績修正', related_symbols=['7203'],
                     facts=['架空資料の記載'], evidence=[body], unknowns=[], numbers=numbers,
                     relations=[dict(symbol='7203', relation='直接', reason=reason)])
        response = dict(status='completed', output=[dict(type='message', content=[
            dict(type='output_text', text=json.dumps(value))])])
        return parse_response(response, dict(title='テスト', body=body, symbols=['7203']), {'7203': 'トヨタ自動車'})

    def number(self, value, period, quote, role):
        return dict(label='営業利益', value=value, period=period, quote=quote, unit='億円',
                    period_quote=quote if period != '不明' else '', role=role)

    def test_revision_same_period_before_after(self):
        body = '架空資料。2027年3月期の営業利益予想を従来の100億円から120億円に変更。'
        numbers = [self.number('100', '2027年3月期', body, '変更前'), self.number('120', '2027年3月期', body, '変更後')]
        self.assertEqual(self.result(body, numbers)['quality_warnings'], [])
        numbers[0]['period'] = '不明'
        numbers[0]['period_quote'] = ''
        result = self.result(body, numbers)
        self.assertEqual(len(result['quality_warnings']), 1)
        self.assertEqual(result['numbers'][0]['period'], '不明')  # 勝手に期間を補わない

    def test_distinct_periods_and_actual_vs_forecast(self):
        first, second = '2026年3月期の営業利益実績は100億円。', '2027年3月期の予想は120億円。'
        numbers = [self.number('100', '2026年3月期', first, '実績'), self.number('120', '2027年3月期', second, '予想')]
        self.assertEqual(self.result(first + second, numbers)['quality_warnings'], [])
        for change in ({'period': '2028年3月期'}, {'period_quote': '架空の引用'}, {'role': '買い'}):
            invalid = copy.deepcopy(numbers)
            invalid[0].update(change)
            with self.assertRaises(ValueError):
                self.result(first + second, invalid)

    def test_missing_period_is_allowed_but_generic_reason_flagged(self):
        body = '営業利益予想は120億円。対象期間は記載なし。'
        numbers = [self.number('120', '不明', body, '予想')]
        self.assertEqual(self.result(body, numbers)['quality_warnings'], [])
        self.assertIn('分類名だけ', self.result(body, numbers, reason='直接')['quality_warnings'][0])

    def test_non_financial_news_without_numbers(self):
        body = '架空資料。トヨタ自動車が新製品を発表。発売時期は未定。'
        self.assertEqual(self.result(body, [])['quality_warnings'], [])


if __name__ == '__main__':
    unittest.main()

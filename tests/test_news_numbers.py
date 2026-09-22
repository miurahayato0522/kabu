from decimal import Decimal
import unittest
from news_numbers import yen, calculate, metric


class MoneyTests(unittest.TestCase):
    def test_forecast_label_variants_and_scope_preservation(self):
        for before,after in [('営業利益（前回見通し）','営業利益（今回見通し）'),
                             ('営業利益見通し（前回）','営業利益見通し（今回）')]:
            result,source = self.sample()
            result['numbers'][0]['label'] = before
            result['numbers'][1]['label'] = after
            self.assertEqual(calculate(result,source)['changes'][0]['metric'],'営業利益')
            result['numbers'][0]['label'] = after
            self.assertEqual(calculate(result,source)['changes'],[])
        self.assertEqual(metric('連結営業利益（前回見通し）'),'連結営業利益')
        for label in ('営業利益（単体）','営業利益（調整後）','営業利益率（前回見通し）','営業利益（前期実績）'):
            self.assertIsNone(metric(label))

    def sample(self, before='100億円', after='120億円', periods=('2027年3月期', '2027年3月期')):
        body = f'{periods[0]}の営業利益予想（変更前）は{before}。{periods[1]}の営業利益予想（変更後）は{after}。'
        numbers = [dict(label='営業利益予想（'+role+'）', role=role, value=value, unit='円', period=period, quote=body, period_quote=body)
                   for role, value, period in zip(('変更前','変更後'), (before,after), periods)]
        return {'numbers': numbers}, {'body': body, 'symbols': ['7203']}

    def test_units_and_signs(self):
        for value, unit, expected in [('100億円','円','10000000000'), ('100','億円','10000000000'),
                                      ('１．２兆円','円','1200000000000'), ('1,234','百万円','1234000000'),
                                      ('△10億円','円','-1000000000'), ('赤字10億円','円','-1000000000')]:
            self.assertEqual(yen(value,unit), Decimal(expected))
        for value,unit in [('100億円','百万円'), ('約100','億円'), ('1億2000万円','円'), ('10～20','億円'),
                           ('12,34','円'), ('NaN','円'), ('100','ドル')]:
            with self.assertRaises(ValueError):
                yen(value,unit)

    def test_upward_downward_and_zero(self):
        for a,b,rate,change in [('100億円','120億円','20','増加'), ('100億円','80億円','-20','減少'),
                                ('0円','10億円',None,'増加'), ('-10億円','-5億円',None,'赤字縮小'),
                                ('-10億円','5億円',None,'赤字から黒字'), ('10億円','-5億円','-150','黒字から赤字')]:
            result = calculate(*self.sample(a,b))['changes'][0]
            self.assertEqual(Decimal(result['revision_pct']) if result['revision_pct'] is not None else None,
                             Decimal(rate) if rate is not None else None)
            self.assertEqual(result['change'], change)

    def test_different_periods_duplicates_companies_and_quality_do_not_pair(self):
        self.assertEqual(calculate(*self.sample(periods=('2026年3月期','2027年3月期')))['changes'], [])
        for case in ('duplicate','company','quality','unknown_metric'):
            result, source = self.sample()
            if case == 'duplicate':
                result['numbers'].append(dict(result['numbers'][0]))
            elif case == 'company':
                source['symbols'].append('9984')
            elif case == 'quality':
                result['quality_warnings'] = ['要確認']
            else:
                result['numbers'][0]['label'] = '利益'
            self.assertEqual(calculate(result,source)['changes'], [])

    def test_unsupported_and_uncited_amount_not_used(self):
        result, source = self.sample()
        result['numbers'][0]['value'] = '200億円'
        self.assertEqual(calculate(result,source)['changes'], [])
        result, source = self.sample()
        result['numbers'][0]['period_quote'] = '架空の期間引用'
        self.assertEqual(calculate(result,source)['changes'], [])


if __name__ == '__main__':
    unittest.main()

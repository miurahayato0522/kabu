from contextlib import closing
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

from news_ai import APIError, analyze, call_openai, make_request, open_cache, parse_response, main


class AITests(unittest.TestCase):
    def setUp(self):
        self.article = {'id': 'a1', 'title': 'トヨタ、決算で増益', 'symbols': ['7203']}
        self.names = {'7203': 'トヨタ自動車'}
        self.result = {'summary': 'トヨタの決算で増益と報じている。', 'category': '決算',
                       'related_symbols': ['7203'], 'evidence': ['決算で増益'],
                       'unknowns': ['増益率は本文未取得のため不明'],
                       'facts': ['見出しではトヨタの増益と報じている'],
                       'relations': [{'symbol': '7203', 'relation': '直接', 'reason': 'トヨタの決算が主題'}]}
        self.response = {'id': 'resp_test', 'model': 'gpt-5-mini', 'status': 'completed',
                         'usage': {'input_tokens': 300, 'output_tokens': 200},
                         'output': [{'type': 'message', 'content': [{'type': 'output_text',
                                      'text': json.dumps(self.result)}]}]}

    def test_request_separates_untrusted_content_and_caps_output(self):
        request = make_request(self.article, self.names)
        self.assertFalse(request['store'])
        self.assertEqual(request['model'], 'gpt-5-nano')
        self.assertEqual(request['max_output_tokens'], 2000)
        self.assertNotIn('tools', request)
        self.assertEqual(request['input'][0]['role'], 'user')
        self.assertTrue(request['text']['format']['strict'])

    def test_success_saved_and_same_request_never_recharged(self):
        with tempfile.TemporaryDirectory() as directory, closing(open_cache(Path(directory) / 'cache.db')) as db:
            caller = Mock(return_value=self.response)
            self.assertEqual(analyze(db, self.article, self.names, 'secret', caller=caller), 'ok')
            self.assertEqual(analyze(db, self.article, self.names, 'secret', caller=caller), 'cached:ok')
            caller.assert_called_once()
            saved = db.execute('SELECT result_json,usage_json,request_json FROM ai_analyses').fetchone()
            self.assertEqual(json.loads(saved[0]), self.result)
            self.assertEqual(json.loads(saved[1])['output_tokens'], 200)
            self.assertNotIn('secret', ''.join(saved))

    def test_failed_unknown_and_interrupted_requests_not_retried(self):
        with tempfile.TemporaryDirectory() as directory, closing(open_cache(Path(directory) / 'cache.db')) as db:
            caller = Mock(side_effect=APIError('通信失敗'))
            self.assertEqual(analyze(db, self.article, self.names, 'secret', caller=caller), 'failed')
            self.assertEqual(analyze(db, self.article, self.names, 'secret', caller=caller), 'cached:failed')
            caller.assert_called_once()
            article = dict(self.article, id='a2')
            interrupt = Mock(side_effect=KeyboardInterrupt())
            with self.assertRaises(KeyboardInterrupt):
                analyze(db, article, self.names, 'secret', caller=interrupt)
            self.assertEqual(analyze(db, article, self.names, 'secret', caller=interrupt), 'cached:started')
            interrupt.assert_called_once()

    def test_invalid_evidence_symbol_and_incomplete_rejected(self):
        for changes in ({'evidence': ['架空の数字']}, {'related_symbols': ['9984']}, {'evidence': []}):
            response = dict(self.response, output=[{'type': 'message', 'content': [
                {'type': 'output_text', 'text': json.dumps(dict(self.result, **changes))}]}])
            with self.assertRaises(ValueError):
                parse_response(response, self.article, self.names)
        with self.assertRaises(ValueError):
            parse_response(dict(self.response, status='incomplete'), self.article, self.names)
        with self.assertRaises(ValueError):
            parse_response(dict(self.response, output=[{'type': 'message', 'content': [{'type': 'refusal'}]}]), self.article, self.names)

    def test_http_error_does_not_leak_key(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError('url', 401, 'secret', {}, io.BytesIO(b'secret'))
        with patch('news_ai.urllib.request.build_opener', return_value=opener):
            with self.assertRaises(APIError) as context:
                call_openai(make_request(self.article, self.names), 'secret')
        self.assertNotIn('secret', str(context.exception))
        self.assertIn('401', str(context.exception))

    def test_publisher_only_unknowns_and_relation_consistency(self):
        article = dict(self.article, title=self.article['title'] + ' - 日本経済新聞', publisher='日本経済新聞')
        self.assertNotIn('日本経済新聞', make_request(article, self.names)['input'][0]['content'])
        for changes in ({'evidence': ['日本経済新聞']}, {'unknowns': []}, {'facts': []},
                        {'relations': []}, {'relations': self.result['relations'] * 2},
                        {'relations': [{'symbol': '7203', 'relation': '不明', 'reason': '会社の特定不可'}]}):
            response = dict(self.response, output=[{'type': 'message', 'content': [
                {'type': 'output_text', 'text': json.dumps(dict(self.result, **changes))}]}])
            with self.assertRaises(ValueError):
                parse_response(response, article, self.names)

    def test_unknown_relation_is_not_a_confirmed_symbol(self):
        result = dict(self.result, related_symbols=[], relations=[
            {'symbol': '7203', 'relation': '不明', 'reason': '本文で企業関係を確認する必要あり'}])
        response = dict(self.response, output=[{'type': 'message', 'content': [
            {'type': 'output_text', 'text': json.dumps(result)}]}])
        self.assertEqual(parse_response(response, self.article, self.names)['related_symbols'], [])

    def test_preview_no_network_or_key_and_limit_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text(json.dumps({'symbols': self.names}), encoding='utf-8')
            article = dict(self.article, decision='確認候補', observed_at='2026-09-20')
            with patch('news_ai.snapshot', return_value=[]), patch('news_ai.build_report', return_value={'items': [article]}), \
                 patch('news_ai.call_openai') as api, patch('news_ai.getpass.getpass') as key:
                self.assertEqual(main(['preview', '--config', str(config)]), 0)
                api.assert_not_called()
                key.assert_not_called()
                self.assertEqual(main(['run', '--limit', '21']), 1)


if __name__ == '__main__':
    unittest.main()

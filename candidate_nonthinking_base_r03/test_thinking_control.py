from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "release_2026-10-02_r00/submission"))
sys.path.insert(0, str(ROOT))
import engine as core
from thinking_control import NonThinkingHouseClient


class Handler(BaseHTTPRequestHandler):
    requests = []
    statuses = []

    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        self.requests.append({'path':self.path, 'body':json.loads(raw),
                              'auth':self.headers.get('Authorization')})
        status = self.statuses.pop(0) if self.statuses else 200
        data = json.dumps({'choices':[{'message':{'content':'owned synthetic reply'}}]}).encode()
        self.send_response(status)
        self.send_header('Content-Length', str(len(data)))
        if status == 302:
            self.send_header('Location','https://example.invalid/never-follow')
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class ThinkingControlTests(unittest.TestCase):
    def setUp(self):
        Handler.requests = []
        Handler.statuses = []
        self.server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.env = patch.dict(os.environ, {'MODEL_TOKEN':'SYNTHETIC_THINKING_CONTROL_ONLY',
                    'AGENTHON_HOUSE_MAX_REQUESTS':'25','AGENTHON_HOUSE_MAX_OUTPUT_TOKENS':'4000',
                    'QFBENCH_SEED':'17','http_proxy':'','https_proxy':'',
                    'HTTP_PROXY':'','HTTPS_PROXY':'','ALL_PROXY':'','all_proxy':''}, clear=True)
        self.env.start()
        self.origin = 'http://127.0.0.1:'+str(self.server.server_port)
        self.messages = [{'role':'user','content':'owned synthetic problem'}]

    def tearDown(self):
        self.env.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def client(self, cls=NonThinkingHouseClient, retries=0):
        return cls(self.origin,'synthetic-model',2,max_retries=retries,retry_backoff_sec=0)

    def test_real_wire_has_only_approved_single_control(self):
        client = self.client()
        self.assertEqual(client.complete(self.messages,4000,3),'owned synthetic reply')
        request = Handler.requests[0]
        self.assertEqual(request['path'],'/v1/chat/completions')
        self.assertEqual(request['auth'],'Bearer SYNTHETIC_THINKING_CONTROL_ONLY')
        self.assertEqual(request['body'], {'model':'synthetic-model','messages':self.messages,
            'temperature':0,'seed':17,'max_tokens':4000,'chat_template_kwargs':{'enable_thinking':False}})
        self.assertEqual(client.requests_attempted,1)

    def test_unchanged_base_wire_is_actual_no_option_control(self):
        self.client(core.OpenAICompatibleClient).complete(self.messages,256,3)
        self.assertNotIn('chat_template_kwargs',Handler.requests[0]['body'])

    def test_retry_payload_is_identical_and_attempts_are_not_reset(self):
        Handler.statuses = [429,200]
        client = self.client(retries=1)
        client.complete(self.messages,1024,3)
        self.assertEqual(client.requests_attempted,2)
        self.assertEqual(len(Handler.requests),2)
        self.assertEqual(Handler.requests[0]['body'],Handler.requests[1]['body'])
        client.complete(self.messages,1024,3)
        self.assertEqual(client.requests_attempted,3)

    def test_output_cap_still_applies(self):
        self.client().complete(self.messages,12000,3)
        self.assertEqual(Handler.requests[0]['body']['max_tokens'],4000)

    def test_redirect_refused_without_a_second_request(self):
        Handler.statuses = [302]
        with self.assertRaisesRegex(core.EndpointError,'redirect refused'):
            self.client().complete(self.messages,1024,3)
        self.assertEqual(len(Handler.requests),1)

    def test_request_budget_refuses_transport_after_allowance(self):
        with patch.dict(os.environ,{'AGENTHON_HOUSE_MAX_REQUESTS':'1'}):
            client = self.client()
        client.complete(self.messages,1024,3)
        with self.assertRaisesRegex(core.EndpointError,'allowance exhausted'):
            client.complete(self.messages,1024,3)
        self.assertEqual(len(Handler.requests),1)

    def test_header_url_and_deadline_are_preserved_at_super_boundary(self):
        client = self.client()
        body = {'model':'synthetic-model','messages':copy.deepcopy(self.messages),'max_tokens':100}
        request = urllib.request.Request(self.origin+'/v1/chat/completions',
            data=json.dumps(body).encode(),headers={'Authorization':'Bearer synthetic','Content-Type':'application/json'},
            method='POST')
        request.add_header('Content-Length','1')
        observed = []
        def fake(base_self, changed, deadline):
            observed.append((changed,deadline))
            return 'owned reply'
        with patch.object(core.OpenAICompatibleClient,'_complete_once',fake):
            self.assertEqual(client._complete_once(request,123.5),'owned reply')
            self.assertEqual(client._complete_once(request,123.5),'owned reply')
        self.assertEqual(observed[0][1],123.5)
        self.assertEqual(request.full_url,self.origin+'/v1/chat/completions')
        self.assertEqual(request.get_method(),'POST')
        self.assertEqual(request.get_header('Authorization'),'Bearer synthetic')
        self.assertFalse(request.has_header('Content-length'))
        self.assertEqual(json.loads(request.data),dict(body,chat_template_kwargs={'enable_thinking':False}))

    def test_extra_controls_fail_closed(self):
        request = urllib.request.Request(self.origin+'/v1/chat/completions',
            data=json.dumps({'chat_template_kwargs':{'low_effort':True}}).encode())
        with self.assertRaisesRegex(core.EndpointError,'combined'):
            self.client()._complete_once(request,123)
        self.assertEqual(Handler.requests,[])

    def test_bad_payload_cannot_enter_transport(self):
        for raw in (b'not-json',b'[]',b'null'):
            with self.subTest(raw=raw):
                with self.assertRaises(core.EndpointError):
                    self.client()._complete_once(urllib.request.Request(self.origin,data=raw),123)
        self.assertEqual(Handler.requests,[])


if __name__ == '__main__':
    unittest.main()

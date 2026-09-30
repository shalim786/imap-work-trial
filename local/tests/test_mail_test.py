import asyncio
import contextlib
import io
import json
import unittest
import httpx
from agentmail_imap.adapter import Adapter
from agentmail_imap.request_policy import RequestPolicy
from scripts.send_mail_test import send_batch, received_batch, subject_for


class MailTestTests(unittest.IsolatedAsyncioTestCase):
    def run_spec(self):
        return {'run_id':'synthetic-test','sender':'sender@imap.test','recipient':'recipient@imap.test','count':3,'sent':{}}

    async def test_send_retries_reuse_payload_and_idempotency_and_resume_skips_successes(self):
        run=self.run_spec();requests=[];checkpoints=[]
        def respond(request):
            requests.append(request)
            if len(requests)==1:
                return httpx.Response(429,headers={'Retry-After':'0.001'},json={'code':'rate_limit'})
            return httpx.Response(200,json={'message_id':f'm{len(requests)}','thread_id':'thread'})
        adapter=Adapter('https://api.test/v0','synthetic',transport=httpx.MockTransport(respond),policy=RequestPolicy(1000,1,0.001,0.002))
        self.addAsyncCleanup(adapter.close)
        with contextlib.redirect_stdout(io.StringIO()):
            await send_batch(adapter,run,lambda:checkpoints.append(len(run['sent'])))
            await send_batch(adapter,run,lambda:checkpoints.append(len(run['sent'])))
        self.assertEqual(len(requests),4)
        self.assertEqual(requests[0].headers['idempotency-key'],requests[1].headers['idempotency-key'])
        self.assertEqual(requests[0].content,requests[1].content)
        self.assertEqual(len({r.headers['idempotency-key'] for r in requests}),3)
        self.assertEqual(checkpoints,[1,2,3])
        self.assertEqual([json.loads(r.content)['to'] for r in requests],[['recipient@imap.test']]*4)

    async def test_delivery_verification_pages_and_excludes_trash_and_other_runs(self):
        run=self.run_spec()
        def message(number,identifier,labels=None):
            return {'message_id':identifier,'subject':subject_for(run,number),'labels':labels or ['received'],
                    'timestamp':'2026-09-29T00:00:00Z','size':1}
        def respond(request):
            if request.url.params.get('page_token'):
                return httpx.Response(200,json={'count':3,'messages':[message(2,'b'),message(3,'c'),message(1,'a')]})
            return httpx.Response(200,json={'count':2,'messages':[message(1,'a'),message(2,'trash',['received','trash'])],'next_page_token':'next'})
        adapter=Adapter('https://api.test/v0','synthetic',transport=httpx.MockTransport(respond),policy=RequestPolicy(1000))
        self.addAsyncCleanup(adapter.close)
        self.assertEqual(await received_batch(adapter,run),{'1':['a'],'2':['b'],'3':['c']})

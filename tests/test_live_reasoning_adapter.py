"""Request fences and optional nonstream presentation; no live provider calls."""
import contextvars
import importlib
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import live_reasoning as bridge
from stream import collect_stream_completion
from agent.subagent_lifecycle import bind_subagent_parent
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.a = NS(provider=bridge.PROVIDER, session_id='session', model='model',
                    _current_turn_id='turn', _current_api_request_id='req',
                    reasoning_callback=self.events.append, _stream_callback=lambda x: None,
                    _model_request_active=threading.Event())
        self.a._model_request_active.set()
        self.client = NS(is_closed=False)
        self.p = patch.object(bridge, '_live_session', return_value=None)
        self.p.start()
        self.addCleanup(self.p.stop)

    def run_request(self, action, **overrides):
        kw = dict(request={'_agy_session_id': self.a.session_id}, next_call=action,
                  provider=bridge.PROVIDER, session_id=self.a.session_id, turn_id='turn',
                  api_request_id='req', model='model')
        kw.update(overrides)
        with bind_subagent_parent(self.a):
            return bridge.execution_middleware(**kw)

    def emitter(self, req):
        return bridge.collector_emitter(req.get('_agy_session_id'), req.get('_agy_request_token'), client=self.client)

    def test_collect_before_completion_preserves_reasoning_answer_tools_usage(self):
        usage = NS(prompt_tokens=3, completion_tokens=4, total_tokens=7,
                   completion_tokens_details=NS(reasoning_tokens=2))
        def chunks():
            yield NS(choices=[NS(delta=NS(reasoning_content='reason'))])
            self.assertEqual(self.events, ['reason'])
            yield NS(choices=[NS(delta=NS(content='answer', tool_calls=[NS(id='call', function=NS(name='tool', arguments='{}'))]), finish_reason='tool_calls')])
            yield NS(choices=[], usage=usage)
        def call(req):
            class Stream:
                model = 'model'
                def __iter__(self): return chunks()
            return collect_stream_completion(Stream(), reasoning_emitter=self.emitter(req))
        response = self.run_request(call)
        self.assertEqual(response.choices[0].message.reasoning_content, 'reason')
        self.assertEqual(response.choices[0].message.content, 'answer')
        self.assertEqual(response.choices[0].message.tool_calls[0].id, 'call')
        self.assertIs(response.usage, usage)

    def test_revoked_late_worker_and_retry(self):
        held = []
        def call(req):
            held.append((contextvars.copy_context(), self.emitter(req)))
            held[-1][1]('first')
        self.run_request(call)
        held[0][0].run(held[0][1], 'late')
        self.a._current_api_request_id = 'retry'
        self.run_request(lambda req: self.emitter(req)('retry'), api_request_id='retry')
        held[0][0].run(held[0][1], 'late-again')
        self.assertEqual(self.events, ['first', 'retry'])

    def test_exception_revokes_without_swallowing_provider_failure(self):
        held = []
        def call(req):
            held.append(self.emitter(req))
            raise RuntimeError('provider failure')
        with self.assertRaisesRegex(RuntimeError, 'provider failure'):
            self.run_request(call)
        held[0]('late')
        self.assertEqual(self.events, [])

    def test_callback_failure_optional_and_once(self):
        def broken(text):
            self.events.append(text)
            raise ValueError('UI gone')
        self.a.reasoning_callback = broken
        def call(req):
            emit = self.emitter(req)
            emit('one'); emit('two')
            return 'answer'
        self.assertEqual(self.run_request(call), 'answer')
        self.assertEqual(self.events, ['one'])

    def test_missing_identity_streaming_aux_other_provider_and_host(self):
        for kw in [dict(request={}), dict(request={'_agy_session_id':'wrong'}),
                   dict(request={'_agy_session_id':'session', 'stream':True}),
                   dict(aux_task='title'), dict(provider='other'), dict(turn_id=''),
                   dict(api_request_id=''), dict(model='other')]:
            with self.subTest(kw=kw):
                self.run_request(lambda req: self.assertIsNone(self.emitter(req)), **kw)
        with bind_subagent_parent(None):
            bridge.execution_middleware(request={'_agy_session_id':'session'},
                next_call=lambda req: self.assertNotIn('_agy_request_token',req),
                provider=bridge.PROVIDER, session_id='session', turn_id='turn', api_request_id='req',model='model')

    def test_each_dynamic_fence(self):
        for name, value in [('provider','other'), ('session_id','other'), ('model','other'),
                            ('_current_turn_id','other'), ('_current_api_request_id','other'),
                            ('reasoning_callback',None), ('_interrupt_requested',True),
                            ('_mute_notification_reply',True), ('_closed',True),
                            ('is_subagent',True), ('side_agent',True), ('platform','cron'), ('_stream_callback',None)]:
            with self.subTest(name=name):
                sentinel = object(); old = getattr(self.a,name,sentinel)
                def call(req):
                    emit = self.emitter(req)
                    setattr(self.a,name,value)
                    emit('forbidden')
                self.run_request(call)
                if old is sentinel: delattr(self.a,name)
                else: setattr(self.a,name,old)
        self.assertEqual(self.events, [])

    def test_closed_client_inactive_request_and_one_claim(self):
        def call(req):
            emit = self.emitter(req)
            self.assertIsNone(self.emitter(req))
            self.client.is_closed = True; emit('closed')
            self.client.is_closed = False
            self.a._model_request_active.clear(); emit('inactive')
        self.run_request(call)
        self.assertEqual(self.events, [])

    def test_scalar_token_cannot_be_reused_by_aux_or_foreign_request(self):
        def call(req):
            self.assertIsNone(bridge.collector_emitter('session','wrong',client=self.client))
            self.assertIsNone(bridge.collector_emitter('other',req['_agy_request_token'],client=self.client))
            bridge.execution_middleware(request={}, next_call=lambda _: self.assertIsNone(self.emitter(req)))
            self.emitter(req)('valid')
        self.run_request(call)
        self.assertEqual(self.events,['valid'])

    def test_live_session_closing_replaced_and_finalized(self):
        for flag in ['_closing', '_finalized', 'replacement', 'agent', 'running']:
            s = {'agent':self.a, 'running':True}
            current = ['sid',s]
            with patch.object(bridge, '_live_session', side_effect=lambda *args:tuple(current)):
                def call(req):
                    emit = self.emitter(req)
                    if flag == 'replacement': current[1] = dict(s)
                    elif flag == 'agent': s['agent'] = object()
                    elif flag == 'running': s['running'] = False
                    else: s[flag] = True
                    emit('forbidden')
                self.run_request(call)
        self.assertEqual(self.events,[])

    def test_context_mismatch_never_falls_back_to_another_agent(self):
        other = NS(**vars(self.a)); other.session_id = 'other'
        with patch.object(bridge, '_live_session', return_value=('sid', {'agent':self.a})), bind_subagent_parent(other):
            agent, _ = bridge._resolve_agent('session',bridge._home())
            self.assertIsNone(agent)

    def test_concurrent_profiles_sessions_and_worker_context(self):
        barrier = threading.Barrier(2)
        with tempfile.TemporaryDirectory() as root:
            def run(index):
                token = set_hermes_home_override(Path(root)/str(index))
                events = []
                a = NS(**vars(self.a)); a.session_id = 'same-id'; a.reasoning_callback = events.append
                def next_call(req):
                    context = contextvars.copy_context()
                    def worker():
                        barrier.wait(timeout=3)
                        emit = bridge.collector_emitter('same-id',req['_agy_request_token'],client=self.client)
                        emit(str(index))
                        foreign = set_hermes_home_override(Path(root)/str(1-index))
                        try: emit('foreign')
                        finally: reset_hermes_home_override(foreign)
                    thread = threading.Thread(target=context.run,args=(worker,)); thread.start();thread.join(5)
                    self.assertFalse(thread.is_alive())
                try:
                    with bind_subagent_parent(a):
                        bridge.execution_middleware(request={'_agy_session_id':'same-id'},next_call=next_call,
                            provider=bridge.PROVIDER,session_id='same-id',turn_id='turn',api_request_id='req',model='model')
                    return events
                finally: reset_hermes_home_override(token)
            with ThreadPoolExecutor(2) as pool:
                results = list(pool.map(run,range(2)))
            self.assertEqual(results,[['0'],['1']])

    def test_absent_tui_session_and_profile_mismatch_fail_closed(self):
        self.a.platform = 'tui'
        self.run_request(lambda req:self.assertIsNone(self.emitter(req)))
        other = NS(**vars(self.a))
        with patch.object(bridge,'_live_session',return_value=('sid',{'agent':other,'running':True})):
            self.run_request(lambda req:self.assertIsNone(self.emitter(req)))

    def test_gateway_fallback_uses_explicit_profile_never_unscoped(self):
        self.p.stop()
        calls = []
        a = self.a
        def finder(key,home):
            calls.append((key,home))
            return ('sid',{'agent':a,'running':True}) if home is None else None
        server = NS(_find_live_session_by_key=finder,_launch_home=lambda:Path(bridge._home()))
        with patch.dict(sys.modules,{'tui_gateway.server':server}), bind_subagent_parent(None):
            agent,live = bridge._resolve_agent('session',bridge._home())
            self.assertIs(agent,a)
            self.assertEqual(calls,[('session',bridge._home()),('session',None)])
            calls.clear()
            self.assertIsNone(bridge._live_session('session','/different-profile'))
            self.assertEqual(calls,[('session','/different-profile')])


if __name__ == '__main__':
    unittest.main()

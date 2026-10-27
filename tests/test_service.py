import asyncio

import pytest

from xmu.store import Store
from xmu.service import Service
from xmu.platform import AuthExpired, ProtocolError


class School:
    def __init__(self, outcome='present'):
        self.calls = 0
        self.outcome = outcome
    async def profile(self):
        return {'id': 42, 'user_no': 'student', 'name': '本人'}
    async def active(self):
        return [{'rollcall_id': 10, 'course_id': 7, 'course_title': '电磁学', 'is_number': True, 'status': 'absent', 'number_code': '0012'}]
    async def detail(self, rid):
        status = 'mystery' if str(rid) == '11' else (self.outcome if self.calls else 'absent')
        return {'student_rollcalls': [{'user_id': 42, 'user_no': 'student', 'status': status}]}
    async def number_code(self, event, detail):
        return '0012'
    async def answer_number(self, rid, code, device):
        self.calls += 1
        if self.outcome == 'timeout':
            raise ProtocolError('timeout')
        return {'success': True}
    async def courses(self):
        return [{'id': 7, 'name': '电磁学'}]
    async def history(self, course_id, user_id):
        return [{'rollcall_id': 10, 'status': 'absent'}, {'rollcall_id': 11, 'status': 'mystery'}, {'rollcall_id': 12, 'status': 'leave'}]


def prepared(tmp_path, api):
    store = Store(tmp_path)
    store.set_secret({'cookie': 'session=secret', 'username': 'student'})
    store.set_profile({'id': '42', 'username': 'student', 'name': '本人'})
    store.update_settings({'enabled': True, 'auto_number': True, 'wait_for_count': 0})
    return store, Service(store, api=api)


def test_verified_success_and_no_duplicate_after_restart(tmp_path):
    api = School()
    store, service = prepared(tmp_path, api)
    async def check():
        await service.poll()
        await service.poll()
        await Service(Store(tmp_path), api=api).poll()
    asyncio.run(check())
    assert api.calls == 1
    assert store.attempt('10')['outcome'] == 'confirmed'
    assert store.records('7')[0]['status'] == 'present'


def test_200_response_without_verified_status_is_not_success(tmp_path):
    api = School('mystery')
    store, service = prepared(tmp_path, api)
    asyncio.run(service.poll())
    assert store.attempt('10')['outcome'] == 'unconfirmed'
    assert store.records('7')[0]['status'] != 'present'


def test_timeout_never_resends_blindly(tmp_path):
    api = School('timeout')
    store, service = prepared(tmp_path, api)
    async def check():
        await service.poll()
        await service.poll()
    asyncio.run(check())
    assert api.calls == 1
    assert store.attempt('10')['outcome'] == 'unconfirmed'


def test_current_absence_is_pending_and_history_unknown_not_absent(tmp_path):
    api = School('mystery')
    store, service = prepared(tmp_path, api)
    store.update_settings({'auto_number': False})
    asyncio.run(service.poll())
    assert store.records('7')[0]['status'] == 'pending'
    asyncio.run(service.sync())
    summary = store.courses()[0]
    assert summary['absent'] == 1
    assert summary['unknown'] == 1
    assert summary['leave'] == 1


def test_partial_history_preserves_existing_and_marks_incomplete(tmp_path):
    api = School()
    store, service = prepared(tmp_path, api)
    asyncio.run(service.sync())
    async def broken(*args):
        raise ProtocolError('page 2 failed', [{'rollcall_id': 20, 'status': 'present'}])
    api.history = broken
    asyncio.run(service.sync())
    course = store.courses()[0]
    assert course['total'] == 4
    assert course['sync_complete'] == 0
    assert course['sync_error']


def test_secret_encrypted_and_account_data_isolated(tmp_path):
    store, _ = prepared(tmp_path, School())
    assert b'session=secret' not in (tmp_path/'attendance.sqlite3').read_bytes()
    asyncio.run(Service(store, api=School()).sync())
    store.set_profile({'id': '99', 'username': 'another'})
    assert store.courses() == []


def test_auth_expiry_stops_writes(tmp_path):
    api = School()
    store, service = prepared(tmp_path, api)
    async def expired():
        raise AuthExpired('expired')
    api.active = expired
    with pytest.raises(AuthExpired):
        asyncio.run(service.poll())
    assert api.calls == 0


def test_pause_prevents_submission(tmp_path):
    api = School()
    store, service = prepared(tmp_path, api)
    store.update_settings({'enabled': False})
    asyncio.run(service.poll())
    assert api.calls == 0


def test_database_claim_atomic(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store, _ = prepared(tmp_path, School())
    with ThreadPoolExecutor(8) as pool:
        claims = list(pool.map(lambda _: store.claim('100'), range(20)))
    assert sum(claims) == 1


def test_worker_survives_unexpected_renew_failure(tmp_path):
    store, service = prepared(tmp_path, School())
    async def expired():
        raise AuthExpired('expired')
    async def malformed():
        raise ValueError('malformed refresh response')
    service.sync = expired
    service.renew = malformed
    async def check():
        task = asyncio.create_task(service.worker())
        await asyncio.sleep(0.03)
        service.stop.set()
        await task
    asyncio.run(check())
    assert service.state['error']


@pytest.mark.parametrize('action',['connect','renew'])
def test_login_profile_cookie_rotation_persisted(tmp_path, monkeypatch, action):
    import httpx
    from xmu import auth
    store, service = prepared(tmp_path, School())
    service.injected_api = None
    store.set_secret({'cookie':'session=old','username':'student','refresh_token':'fixture'})
    async def fake_login(*args):
        return {'cookie':'session=old','refresh_token':'new-fixture'}
    monkeypatch.setattr(auth,'login',fake_login)
    async def check():
        await service.http.aclose()
        service.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(
            200,json={'id':42,'user_no':'student'},headers={'set-cookie':'session=renewed; Path=/; Secure'})))
        if action == 'connect':
            await service.connect(cookie='session=old')
        else:
            await service.renew()
        await service.close()
    asyncio.run(check())
    assert store.secret()['cookie'] == 'session=renewed'


def test_finished_history_absence_not_pending_even_when_not_expired(tmp_path):
    api=School()
    store,service=prepared(tmp_path,api)
    async def history(*args):
        return [{'rollcall_id':10,'status':'absent','rollcall_status':'finished','is_expired':False}]
    api.history=history
    asyncio.run(service.sync())
    assert store.records('7')[0]['status']=='absent'


def test_finished_rollcall_never_submitted(tmp_path):
    api=School()
    store,service=prepared(tmp_path,api)
    async def active():
        return [{'rollcall_id':10,'course_id':7,'is_number':True,'status':'absent','rollcall_status':'finished','is_expired':False}]
    api.active=active
    asyncio.run(service.poll())
    assert api.calls == 0


@pytest.mark.parametrize('event_kind',['number','radar'])
def test_wait_for_ten_then_submit_without_claiming_early(tmp_path,event_kind):
    api=School();store,service=prepared(tmp_path,api)
    store.update_settings({'wait_for_count':10})
    signed=[9]
    original_active=api.active
    async def active():
        events=await original_active()
        events[0]['is_radar']=event_kind=='radar'
        return events
    async def answer_radar(*args):
        api.calls+=1
        return {'accepted':True}
    api.active=active
    api.answer_radar=answer_radar
    async def detail(rid):
        return {'student_rollcalls':[{'user_id':42,'status':'present' if api.calls else 'absent'}]+[
            {'user_id':100+i,'status':'on_call_fine'} for i in range(signed[0])]}
    api.detail=detail
    async def check():
        await service.poll()
        assert api.calls==0 and store.attempt('10') is None
        assert '9/10' in service.state['active'][0]['result']
        signed[0]=10
        await service.poll()
        await service.poll()
    asyncio.run(check())
    assert api.calls==1
    assert store.attempt('10')['outcome']=='confirmed'


def test_no_roster_means_wait_not_early_submission(tmp_path):
    api=School();store,service=prepared(tmp_path,api)
    store.update_settings({'wait_for_count':10})
    async def detail(rid):return None
    api.detail=detail
    asyncio.run(service.poll())
    assert api.calls==0
    assert store.attempt('10') is None


def test_wait_threshold_persisted_across_store_reload(tmp_path):
    store=Store(tmp_path)
    assert store.settings()['wait_for_count']==10
    store.update_settings({'wait_for_count':12})
    assert Store(tmp_path).settings()['wait_for_count']==12

import asyncio
import math

import httpx
import pytest

from xmu.platform import Platform, AuthExpired, ProtocolError, Rejected, normalize_status, own_status, extract_code
from xmu.geo import candidates, distance
from xmu.auth import encrypt_password


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize('raw,expected', [
    ('on_call_fine', 'present'), ('absent', 'absent'), ('on_call_late', 'late'),
    ('leave', 'leave'), ('not_signed', 'absent'), ('已签到', 'present'),
    ('mystery', 'unknown'), (None, 'unknown'), (1, 'unknown'),
])
def test_status(raw, expected):
    assert normalize_status(raw) == expected


def test_only_own_record_counts():
    payload = {'student_rollcalls': [
        {'user_id': 42, 'user_no': '2026001', 'status': 'on_call_fine'},
        {'user_id': 43, 'user_no': '2026002', 'status': 'absent'},
    ]}
    assert own_status(payload, '42', '2026001') == 'present'
    assert own_status(payload, '99', 'missing') == 'unknown'


def test_number_code_keeps_leading_zeroes():
    assert extract_code({'data': {'number_code': '0012'}}) == '0012'
    assert extract_code({'number_code': None}) == ''


def test_reject_login_html_even_when_200():
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, text='<html>authserver login</html>')
        )) as http:
            await Platform(http, 'session=private').profile()
    with pytest.raises(AuthExpired):
        run(check())


def test_full_history_pagination():
    seen = []
    def handler(request):
        p = int(request.url.params.get('page', '1')); seen.append(p)
        items = [{'rollcall_id': p, 'status': 'absent'}] if p < 4 else []
        return httpx.Response(200, json={'rollcalls': items, 'pagination': {'total_pages': 3}})
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await Platform(http, 'x=y').history('7', '42')
    assert len(run(check())) == 3
    assert seen == [1, 2, 3]


def test_repeated_page_is_error_not_complete_history():
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={'rollcalls': [{'rollcall_id': 1}], 'total': 500})
        )) as http:
            return await Platform(http, 'x=y').history('7', '42')
    with pytest.raises(ProtocolError):
        run(check())


def test_forbidden_detail_does_not_imply_absent():
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(403, json={'error': 'forbidden'})
        )) as http:
            return await Platform(http, 'x=y').detail('1')
    assert run(check()) is None


def test_number_payload_and_missing_code_does_not_submit():
    sent = []
    def handler(request):
        import json
        sent.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={'success': True})
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            api = Platform(http, 'x=y')
            await api.answer_number('15', '0042', 'device')
            with pytest.raises(ValueError):
                await api.answer_number('15', '', 'device')
    run(check())
    assert sent == [('PUT', '/api/rollcall/15/answer_number_rollcall', {'deviceId': 'device', 'numberCode': '0042'})]


def test_radar_geometry_contains_target():
    a, b, target = (24.3, 118.0), (24.6, 118.2), (24.437, 118.103)
    points = candidates(a, b, distance(a, target), distance(b, target))
    assert any(distance(p, target) < 120 for p in points)
    assert candidates(a, b, -1, math.nan) == []


def test_cas_encryption_has_random_prefix_and_iv():
    a = encrypt_password('test-password', '1234567890123456')
    b = encrypt_password('test-password', '1234567890123456')
    assert a != b
    assert 'test-password' not in a


def test_never_send_cookie_to_untrusted_url():
    async def check():
        async with httpx.AsyncClient() as http:
            await Platform(http, 'secret=value').request('GET', 'https://evil.example/api/profile')
    with pytest.raises(ValueError):
        run(check())


def test_history_forbidden_is_course_permission_not_expired_login():
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403, json={'error':'forbidden'}))) as http:
            await Platform(http, 'x=y').history('7','42')
    with pytest.raises(Rejected):
        run(check())


def test_premature_empty_page_does_not_claim_complete_history():
    def handler(request):
        p = int(request.url.params['page'])
        return httpx.Response(200, json={'rollcalls':[{'rollcall_id':1}] if p == 1 else [], 'total_pages':3, 'total':3})
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await Platform(http,'x=y').history('7','42')
    with pytest.raises(ProtocolError) as error:
        run(check())
    assert len(error.value.partial) == 1


def test_invalid_refresh_response_is_a_protocol_error():
    from xmu.auth import login
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200,text='bad gateway'))) as http:
            await login(http,'student','','refresh-token-fixture')
    with pytest.raises(ProtocolError):
        run(check())


def test_rotated_cookie_is_saved_for_next_request():
    saved, headers = [], []
    def handler(request):
        headers.append(request.headers.get('cookie'))
        return httpx.Response(200,json={'id':42},headers={'set-cookie':'session=renewed; Path=/; Secure; HttpOnly'})
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            api = Platform(http, 'session=old', on_cookie=saved.append)
            await api.profile(); await api.profile()
    run(check())
    assert headers[1] == 'session=renewed'
    assert saved == ['session=renewed']


def test_history_supports_unpaged_endpoint_after_bare_request_confirmation():
    calls=[]
    def handler(request):
        calls.append(dict(request.url.params))
        return httpx.Response(200,json={'rollcalls':[{'rollcall_id':1},{'rollcall_id':2}]})
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await Platform(http,'x=y').history('7','42')
    assert len(run(check())) == 2
    assert calls[-1] == {}


def test_signed_count_deduplicates_and_excludes_self_absent_leave():
    from xmu.platform import signed_count
    roster={'student_rollcalls':[
        {'user_id':42,'status':'present'},
        {'user_id':100,'user_no':'s100','status':'on_call_fine'},
        {'user_no':'s100','status':'on_call_fine'},
        {'user_id':101,'status':'late'},
        {'user_id':102,'status':'leave'},
        {'user_id':103,'status':'absent'},
        {'status':'on_call_fine'},
    ]}
    assert signed_count(roster,'42','student')==2
    assert signed_count(None,'42','student') is None

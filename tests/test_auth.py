import asyncio
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from xmu.auth import LoginForm, login, CALLBACK


SCHOOL_HTML = '''<html>
<form id="loginFromId" action="/authserver/login"><input name="execution" value="fido-flow"></form>
<form id="phoneFromId" action="/authserver/login"><input name="execution" value="sms-flow"></form>
<form id="pwdFromId" action="/authserver/login">
<input id="pwdEncryptSalt" value="1234567890123456">
<input name="execution" value="password-flow"><input name="cllt" value="userNameLogin">
<input name="dllt" value="generalLogin"><input name="lt" value="">
<input name="password" id="saltPassword" value="">
</form>
<form id="qrLoginForm" action="/authserver/login"><input name="execution" value="qr-flow"></form>
</html>'''


def test_password_form_not_overwritten_by_qr_or_sms_form():
    form = LoginForm(SCHOOL_HTML)
    assert form.values['execution'] == 'password-flow'
    assert form.values['cllt'] == 'userNameLogin'


def test_cas_submission_keeps_service_query_added_by_school_javascript():
    submitted = []
    def handler(request):
        url = request.url
        if url.host == 'c-identity.xmu.edu.cn' and url.path.endswith('/auth'):
            return httpx.Response(303, headers={'location':'https://ids.xmu.edu.cn/authserver/login?service=https%3A%2F%2Fc-identity.xmu.edu.cn%2Fbroker%2Fcas'})
        if url.host == 'ids.xmu.edu.cn' and request.method == 'GET':
            return httpx.Response(200, text=SCHOOL_HTML)
        if url.host == 'ids.xmu.edu.cn' and request.method == 'POST':
            submitted.append(request)
            return httpx.Response(302, headers={'location': CALLBACK+'&code=test-code'})
        if url.path.endswith('/token'):
            return httpx.Response(200, json={'access_token':'test-access','refresh_token':'test-refresh'})
        if url.host == 'lnt.xmu.edu.cn':
            return httpx.Response(200, json={'success':True}, headers={'set-cookie':'session=test-session; Path=/; Secure'})
        raise AssertionError(str(url))
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as h:
            return await login(h,'synthetic-student','synthetic-password')
    result=asyncio.run(check())
    assert result['cookie']=='session=test-session'
    request=submitted[0]
    assert request.url.params.get('service')=='https://c-identity.xmu.edu.cn/broker/cas'
    fields=parse_qs(request.content.decode(),keep_blank_values=True)
    assert fields['execution']==['password-flow']
    assert fields['cllt']==['userNameLogin']
    assert fields['password'][0] != 'synthetic-password'

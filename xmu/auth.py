import base64
from html.parser import HTMLParser
import secrets
from urllib.parse import urljoin, urlsplit, parse_qs, parse_qsl, urlencode, urlunsplit

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from .platform import AuthExpired, ProtocolError

OIDC = 'https://c-identity.xmu.edu.cn/auth/realms/xmu/protocol/openid-connect/'
CALLBACK = 'https://c-mobile.xmu.edu.cn/identity-web-login-callback?_h5=true'
HOSTS = {'ids.xmu.edu.cn', 'c-identity.xmu.edu.cn', 'c-mobile.xmu.edu.cn', 'lnt.xmu.edu.cn'}


def encrypt_password(password, salt):
    alphabet = 'ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz2345678'
    prefix = ''.join(secrets.choice(alphabet) for _ in range(64))
    iv = ''.join(secrets.choice(alphabet) for _ in range(16)).encode()
    padder = PKCS7(128).padder()
    plain = padder.update((prefix+password).encode())+padder.finalize()
    enc = Cipher(algorithms.AES(salt.encode()), modes.CBC(iv)).encryptor()
    return base64.b64encode(enc.update(plain)+enc.finalize()).decode()


class LoginForm(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.forms, self.current, self.global_values = [], None, {}
        self.feed(html)
        preferred = next((f for f in self.forms if f['id'] == 'pwdFromId'), None)
        if preferred is None:
            preferred = next((f for f in self.forms if f['values'].get('cllt') == 'userNameLogin'), None)
        if preferred is None:
            preferred = next((f for f in self.forms if 'pwdEncryptSalt' in f['values']), None)
        if preferred is None:
            preferred = next(iter(self.forms), {'values': self.global_values, 'action': ''})
        self.values = preferred['values'].copy()
        if 'pwdEncryptSalt' not in self.values and self.global_values.get('pwdEncryptSalt'):
            self.values['pwdEncryptSalt'] = self.global_values['pwdEncryptSalt']
        self.action = preferred['action']

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'form':
            self.current = {'id': a.get('id', ''), 'action': a.get('action', ''), 'values': {}}
            self.forms.append(self.current)
        if tag == 'input':
            values = self.current['values'] if self.current is not None else self.global_values
            for key in ('name', 'id'):
                if a.get(key):
                    values[a[key]] = a.get('value', '')

    def handle_endtag(self, tag):
        if tag == 'form':
            self.current = None


def submission_url(current_url, action):
    """Match the school's JS: preserve the CAS service in the form action."""
    target = urlsplit(urljoin(current_url, action) if action else current_url)
    query = parse_qsl(target.query, keep_blank_values=True)
    keys = {k for k,v in query}
    # The browser's login.js appends this from the current login-page query.
    for key, value in parse_qsl(urlsplit(current_url).query, keep_blank_values=True):
        if key == 'service' and key not in keys:
            query.append((key, value))
    return urlunsplit((target.scheme, target.netloc, target.path, urlencode(query), target.fragment))


async def login(http, username, password, refresh_token=''):
    async def request(method, url, **kwargs):
        p = urlsplit(url)
        if p.scheme != 'https' or p.hostname not in HOSTS:
            raise ProtocolError('学校登录跳转到未知域名，已停止')
        try:
            r = await http.request(method, url, follow_redirects=False, **kwargs)
        except httpx.HTTPError as e:
            raise ProtocolError('学校身份认证服务连接失败') from e
        return r

    token = None
    if refresh_token:
        r = await request('POST', OIDC+'token', data={'client_id': 'TronClassH5', 'grant_type': 'refresh_token', 'refresh_token': refresh_token})
        if r.status_code == 200:
            try:
                token = r.json()
            except ValueError as e:
                raise ProtocolError('学校令牌服务返回无法识别的响应') from e
            if not isinstance(token, dict):
                raise ProtocolError('学校令牌响应格式变化')
    if token is None:
        if not username or not password:
            raise AuthExpired('需要重新提供学校账号登录')
        r = await request('GET', OIDC+'auth', params={'scope': 'openid', 'response_type': 'code', 'client_id': 'TronClassH5', 'redirect_uri': CALLBACK})
        for _ in range(10):
            if r.is_redirect:
                r = await request('GET', urljoin(str(r.url), r.headers['location']))
            else:
                break
        form = LoginForm(r.text)
        salt = form.values.get('pwdEncryptSalt')
        execution = form.values.get('execution')
        if not salt or not execution:
            raise AuthExpired('未能读取学校登录页面；可能需要验证码或登录流程已变化')
        action = submission_url(str(r.url), form.action)
        r = await request('POST', action, data={
            'username': username, 'password': encrypt_password(password, salt),
            'captcha': '', '_eventId': 'submit', 'cllt': 'userNameLogin',
            'dllt': 'generalLogin', 'lt': form.values.get('lt', ''), 'execution': execution,
        })
        code = ''
        for _ in range(12):
            if not r.is_redirect:
                raise AuthExpired('学校登录未成功，请检查账号密码；若要求验证码，请使用 Cookie 登录')
            next_url = urljoin(str(r.url), r.headers.get('location', ''))
            if urlsplit(next_url).hostname == 'c-mobile.xmu.edu.cn':
                code = parse_qs(urlsplit(next_url).query).get('code', [''])[0]
                if code:
                    break
            r = await request('GET', next_url)
        if not code:
            raise AuthExpired('学校登录回调缺少授权码')
        r = await request('POST', OIDC+'token', data={
            'client_id': 'TronClassH5', 'grant_type': 'authorization_code',
            'code': code, 'redirect_uri': CALLBACK, 'scope': 'openid',
        })
        try:
            token = r.json()
        except ValueError as e:
            raise AuthExpired('学校认证未返回令牌') from e
        if not isinstance(token, dict):
            raise ProtocolError('学校令牌响应格式变化')
    if not token.get('access_token'):
        raise AuthExpired('学校认证未返回可用令牌')
    r = await request('POST', 'https://lnt.xmu.edu.cn/api/login?login=access_token', json={'access_token': token['access_token'], 'org_id': 1})
    if not 200 <= r.status_code < 300:
        raise AuthExpired('课程平台登录未成功')
    cookie = '; '.join(f'{c.name}={c.value}' for c in http.cookies.jar if c.domain.lstrip('.') in ('lnt.xmu.edu.cn', 'xmu.edu.cn'))
    if not cookie:
        raise AuthExpired('课程平台未建立会话')
    return {'cookie': cookie, 'refresh_token': token.get('refresh_token', refresh_token)}

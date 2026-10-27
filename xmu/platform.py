from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit
from http.cookies import SimpleCookie
import re

import httpx

BASE = 'https://lnt.xmu.edu.cn'
SHANGHAI = timezone(timedelta(hours=8))
STATUS_KEYS = ('rollcall_status', 'student_rollcall_status', 'status', 'state')
CODE_KEYS = ('number_code', 'numberCode', 'rollcall_number_code', 'rollcallNumberCode')


class AuthExpired(Exception):
    pass


class ProtocolError(Exception):
    def __init__(self, message, partial=None):
        super().__init__(message)
        self.partial = partial or []


class Rejected(Exception):
    pass


def normalize_status(raw):
    if not isinstance(raw, str):
        return 'unknown'
    s = re.sub(r'[\s-]+', '_', raw.strip().lower())
    groups = {
        'present': {'on_call_fine', 'fine', 'present', 'signed', 'success', 'attended', 'done', '已签到', '已签', '已到'},
        'absent': {'absent', 'unsigned', 'not_signed', 'unanswered', 'missed', 'miss', '缺勤', '未签到', '未签', '未到'},
        'leave': {'leave', 'on_leave', 'on_call_leave', 'excused', '请假', '病假', '事假'},
        'late': {'late', 'on_call_late', '迟到'},
    }
    return next((key for key, vals in groups.items() if s in vals), 'unknown')


def status_of(row):
    for key in STATUS_KEYS:
        if key in row:
            val = normalize_status(row[key])
            if val != 'unknown':
                return val
    return 'unknown'


def own_status(payload, user_id, username):
    if not isinstance(payload, dict):
        return 'unknown'
    for row in rows(payload, ('student_rollcalls', 'students', 'items', 'list')):
        nested = row.get('user') or row.get('student') or {}
        if not isinstance(nested, dict):
            nested = {}
        ids = [row.get(k, nested.get(k)) for k in ('user_id', 'userId', 'student_id', 'studentId')]
        names = [row.get(k, nested.get(k)) for k in ('user_no', 'student_no', 'username', 'number', 'account')]
        # A generic row.id is the attendance record ID, never the student's ID.
        if (user_id and str(user_id) in [str(x) for x in ids if x is not None]) or (
            username and str(username) in [str(x) for x in names if x is not None]
        ) or row.get('is_current_user') is True or row.get('is_self') is True:
            return status_of(row)
    return 'unknown'


def signed_count(payload, user_id, username):
    """Count distinct arrived classmates, without retaining their roster data."""
    if not isinstance(payload, (dict,list)):
        return None
    if isinstance(payload,dict) and not any(isinstance(payload.get(k),list) for k in ('student_rollcalls','students','items','list')):
        return None
    groups=[]
    for row in rows(payload,('student_rollcalls','students','items','list')):
        nested=row.get('user') or row.get('student') or {}
        if not isinstance(nested,dict):nested={}
        aliases=set()
        for key in ('user_id','userId','student_id','studentId'):
            value=row.get(key,nested.get(key))
            if value is not None and str(value):aliases.add(('id',str(value)))
        for key in ('user_no','student_no','username','number','account'):
            value=row.get(key,nested.get(key))
            if value is not None and str(value):aliases.add(('no',str(value)))
        if not aliases:
            continue
        own=(bool(user_id) and ('id',str(user_id)) in aliases) or (bool(username) and ('no',str(username)) in aliases) or row.get('is_self') is True or row.get('is_current_user') is True
        merged={'aliases':aliases,'own':own,'arrived':status_of(row) in ('present','late')}
        others=[]
        for group in groups:
            if group['aliases'] & merged['aliases']:
                merged['aliases'] |= group['aliases']
                merged['own'] |= group['own']
                merged['arrived'] |= group['arrived']
            else:
                others.append(group)
        groups=others+[merged]
    return sum(g['arrived'] and not g['own'] for g in groups)


def extract_code(obj, depth=0):
    if depth > 8:
        return ''
    if isinstance(obj, dict):
        for key in CODE_KEYS:
            value = obj.get(key)
            if value is not None and str(value).strip() and not isinstance(value, (dict, list, bool)):
                return str(value).strip()
        for value in obj.values():
            code = extract_code(value, depth+1)
            if code:
                return code
    if isinstance(obj, list):
        for value in obj:
            code = extract_code(value, depth+1)
            if code:
                return code
    return ''


def rows(payload, keys):
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in keys:
            if key in payload:
                value = payload[key]
                if isinstance(value, list):
                    return [x for x in value if isinstance(x, dict)]
                if isinstance(value, dict):
                    return rows(value, keys)
    return []


def metadata(payload, key):
    if not isinstance(payload, dict):
        return None
    if key in payload:
        return payload[key]
    for k in ('pagination', 'meta', 'data'):
        val = metadata(payload.get(k), key)
        if val is not None:
            return val
    return None


def timestamp(row):
    return str(next((row[k] for k in ('rollcall_time', 'start_time', 'created_at', 'time') if row.get(k)), ''))


def kind(row):
    if row.get('is_radar') is True:
        return 'radar'
    if row.get('is_number') is True:
        return 'number'
    raw = str(row.get('rollcall_type', row.get('type', ''))).lower()
    if 'radar' in raw:
        return 'radar'
    if 'number' in raw:
        return 'number'
    return 'qr' if row.get('is_qrcode') or row.get('is_qr') or 'qr' in raw or row.get('is_number') is False else 'unknown'


def expired(row):
    # XMU history keeps is_expired=false for manually finished rollcalls.
    if str(row.get('rollcall_status', '')).lower() in ('finished', 'ended', 'closed'):
        return True
    if row.get('is_expired') is True:
        return True
    for key in ('deadline', 'end_time', 'rollcall_end_time', 'expired_at', 'expire_at', 'expires_at'):
        value = row.get(key)
        if not value:
            continue
        try:
            t = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if t.tzinfo is None:
                t = t.replace(tzinfo=SHANGHAI)
            return t <= datetime.now(timezone.utc)
        except (ValueError, TypeError):
            continue
    return False


class Platform:
    def __init__(self, http, cookie, on_cookie=None):
        self.http, self.cookie, self.on_cookie = http, cookie, on_cookie

    async def request(self, method, path, *, allow_forbidden=False, allow_distance=False, **kwargs):
        url = path if path.startswith('https://') else BASE + path
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc != 'lnt.xmu.edu.cn' or not parsed.path.startswith('/api/'):
            raise ValueError('只允许访问厦大课程平台 API')
        try:
            response = await self.http.request(method, url, headers={
                'Cookie': self.cookie, 'Accept': 'application/json, text/plain, */*',
                'User-Agent': 'Mozilla/5.0 (Linux; Android 13) Mobile Safari/537.36',
                'Accept-Language': 'zh-CN,zh;q=0.9',
            }, follow_redirects=False, **kwargs)
        except httpx.HTTPError as e:
            raise ProtocolError('学校平台连接失败或超时；提交结果需回查') from e
        if response.status_code == 401 or 300 <= response.status_code < 400:
            raise AuthExpired('学校登录已失效，请重新登录')
        if response.status_code == 403 and allow_forbidden:
            return None
        content_type = response.headers.get('content-type', '')
        if 'html' in content_type or response.text.lstrip().lower().startswith(('<!doctype', '<html')):
            raise AuthExpired('平台返回登录页面，请重新登录')
        try:
            payload = response.json() if response.content else {}
        except ValueError as e:
            raise ProtocolError('平台未返回可识别的 JSON 数据') from e
        if response.status_code == 403 and method == 'GET':
            raise Rejected('平台未授予此记录的读取权限')
        if allow_distance and isinstance(payload, dict) and isinstance(payload.get('distance'), (int, float)):
            return {'accepted': 200 <= response.status_code < 300 and payload.get('success') is not False, 'distance': payload['distance']}
        if response.status_code >= 400 or (isinstance(payload, dict) and (payload.get('success') is False or payload.get('error'))):
            raise Rejected(f'平台拒绝本次操作（HTTP {response.status_code}）')
        if 200 <= response.status_code < 300 and response.headers.get('set-cookie'):
            jar = SimpleCookie()
            jar.load(self.cookie)
            for name, value in response.cookies.items():
                jar[name] = value
            updated = '; '.join(f'{k}={v.value}' for k,v in jar.items())
            if updated != self.cookie:
                self.cookie = updated
                if self.on_cookie:
                    self.on_cookie(updated)
        return payload

    async def profile(self):
        data = await self.request('GET', '/api/profile')
        if isinstance(data, dict) and isinstance(data.get('data'), dict):
            data = data['data']
        if not isinstance(data, dict) or not any(data.get(k) for k in ('id', 'user_id', 'userId')):
            raise ProtocolError('平台未返回本人用户 ID')
        return data

    async def paginate(self, path, keys, size=100, allow_unpaged=False):
        output, seen = [], set()
        announced_pagination = False
        for page in range(1, 1001):
            payload = await self.request('GET', path, params={'page': page, 'page_size': size, 'per_page': size})
            announced_pagination = announced_pagination or any(metadata(payload, k) is not None for k in ('total_pages','page_count','total','total_count'))
            batch = rows(payload, keys)
            valid_shape = isinstance(payload, list) or any(metadata(payload, key) is not None for key in keys)
            if not valid_shape:
                raise ProtocolError('分页响应缺少记录数组，同步未完成', output)
            if not batch:
                total_pages = metadata(payload, 'total_pages') or metadata(payload, 'page_count')
                total = metadata(payload, 'total') or metadata(payload, 'total_count')
                if (total is not None and len(output) < int(total)) or (total_pages is not None and page <= int(total_pages)):
                    raise ProtocolError('分页提前结束，历史尚未完整读取', output)
                return output
            signature = tuple(str(x.get('rollcall_id', x.get('id', repr(x)))) for x in batch)
            if signature in seen:
                if allow_unpaged and page == 2 and len(output) < size and not announced_pagination:
                    # The student history endpoint may ignore pagination altogether.
                    # Confirm against the bare endpoint; large/capped sets remain unverified.
                    bare = await self.request('GET', path)
                    confirmed = rows(bare, keys)
                    bare_signature = tuple(str(x.get('rollcall_id', x.get('id', repr(x)))) for x in confirmed)
                    if bare_signature == signature and not any(metadata(bare, k) is not None for k in ('total_pages','page_count','total','total_count')):
                        return confirmed
                raise ProtocolError('平台重复返回同一页，无法确认历史完整性', output)
            seen.add(signature); output.extend(batch)
            total_pages = metadata(payload, 'total_pages') or metadata(payload, 'page_count')
            total = metadata(payload, 'total') or metadata(payload, 'total_count')
            if total_pages is not None and page >= int(total_pages):
                return output
            if total is not None and len(output) >= int(total):
                return output
        raise ProtocolError('分页超过限制，同步未完成', output)

    async def courses(self):
        # This APK entry commonly returns all courses without pagination metadata.
        data = await self.request('GET', '/api/my-courses', params={'per_page': 1000, 'page_size': 1000, 'page': 1})
        result = rows(data, ('courses', 'data', 'items', 'results'))
        if not isinstance(data, (list, dict)) or (isinstance(data, dict) and not any(k in data for k in ('courses', 'data', 'items', 'results'))):
            raise ProtocolError('课程列表响应格式变化')
        pages = metadata(data, 'total_pages')
        total = metadata(data, 'total') or metadata(data, 'total_count')
        if (pages and int(pages) > 1) or (total and int(total) > len(result)) or len(result) >= 1000:
            return await self.paginate('/api/my-courses', ('courses', 'data', 'items', 'results'), 1000)
        return result

    async def active(self):
        data = await self.request('GET', '/api/radar/rollcalls')
        if not isinstance(data, dict) or not isinstance(data.get('rollcalls'), list):
            raise ProtocolError('当前签到列表格式变化')
        return data['rollcalls']

    async def detail(self, rollcall_id):
        return await self.request('GET', f'/api/rollcall/{quote(str(rollcall_id), safe="")}/student_rollcalls', allow_forbidden=True)

    async def history(self, course_id, user_id):
        path = f'/api/course/{quote(str(course_id), safe="")}/student/{quote(str(user_id), safe="")}/rollcalls'
        return await self.paginate(path, ('rollcalls', 'activities', 'data', 'items', 'list', 'results'), allow_unpaged=True)

    async def number_code(self, event, detail):
        code = extract_code(event) or extract_code(detail)
        if code:
            return code
        cid = event.get('course_id') or (event.get('course') or {}).get('id')
        if not cid:
            return ''
        date = datetime.now(timezone.utc).date()
        try:
            date = datetime.fromisoformat(timestamp(event).replace('Z', '+00:00')).astimezone(timezone.utc).date()
        except (ValueError, TypeError):
            pass
        rid = str(event.get('rollcall_id', event.get('id', '')))
        for day in (date, date-timedelta(days=1)):
            data = await self.request('GET', '/api/timetable_rollcalls', params={'course_ids': str(cid), 'rollcall_date': day.isoformat()})
            for row in rows(data, ('rollcalls', 'data', 'items')):
                if str(row.get('rollcall_id', row.get('id', ''))) == rid:
                    return extract_code(row)
        return ''

    async def answer_number(self, rid, code, device):
        if not code:
            raise ValueError('缺少签到码')
        return await self.request('PUT', f'/api/rollcall/{quote(str(rid), safe="")}/answer_number_rollcall', json={'deviceId': device, 'numberCode': code})

    async def answer_radar(self, rid, point, device):
        lat, lon = point
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError('坐标超出范围')
        data = await self.request('PUT', f'/api/rollcall/{quote(str(rid), safe="")}/answer', allow_distance=True, json={
            'accuracy': 35, 'altitude': 0, 'altitudeAccuracy': None, 'deviceId': device,
            'heading': None, 'latitude': lat, 'longitude': lon, 'speed': None,
        })
        return data if isinstance(data, dict) and 'accepted' in data else {'accepted': True}

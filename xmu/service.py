import asyncio
import time
import httpx

from . import auth
from .geo import candidates
from .platform import Platform, AuthExpired, ProtocolError, Rejected, own_status, signed_count, status_of, timestamp, kind, expired


class Service:
    def __init__(self, store, api=None):
        self.store = store
        self.injected_api = api
        self.http = httpx.AsyncClient(timeout=20)
        self.lock = asyncio.Lock()
        self.stop = asyncio.Event()
        self.sync_requested = asyncio.Event()
        self.state = {'connected': bool(store.owner and store.secret().get('cookie')),
                      'busy': False, 'last_poll': None, 'last_sync': store.get('last_sync'),
                      'error': '', 'active': []}

    def api(self):
        if self.injected_api is not None:
            return self.injected_api
        secret = self.store.secret()
        if not secret.get('cookie'):
            raise AuthExpired('请先登录学校账号')
        def save_cookie(value):
            current = self.store.secret()
            current['cookie'] = value
            self.store.set_secret(current)
        return Platform(self.http, secret['cookie'], on_cookie=save_cookie)

    async def connect(self, username='', password='', cookie=''):
        async with self.lock:
            if cookie:
                secret = {'username': username.strip(), 'cookie': cookie.strip()}
            else:
                result = await auth.login(self.http, username.strip(), password)
                secret = {'username': username.strip(), 'password': password, **result}
            candidate = Platform(self.http, secret['cookie'])
            profile = await candidate.profile()
            secret['cookie'] = candidate.cookie
            uid = str(profile.get('id') or profile.get('user_id') or profile.get('userId'))
            name = profile.get('name') or profile.get('realname') or ''
            username = str(profile.get('user_no') or profile.get('username') or secret.get('username') or '')
            if self.store.owner != uid:
                self.store.update_settings({'enabled': False})
                self.state['active'] = []
                self.store.put('last_sync', None)
                self.state['last_sync'] = None
            self.store.set_profile({'id': uid, 'username': username, 'name': name})
            self.store.set_secret(secret)
            self.state.update(connected=True, error='')
            self.store.log('学校账号登录成功')
            self.sync_requested.set()
            return self.store.get('profile')

    async def renew(self):
        secret = self.store.secret()
        if not secret.get('refresh_token') and not secret.get('password'):
            raise AuthExpired('学校登录已失效，请重新登录或更新 Cookie')
        async with self.lock:
            result = await auth.login(self.http, secret.get('username',''), secret.get('password',''), secret.get('refresh_token',''))
            candidate = Platform(self.http, result['cookie'])
            profile = await candidate.profile()
            result['cookie'] = candidate.cookie
            uid = str(profile.get('id') or profile.get('user_id') or profile.get('userId'))
            if uid != self.store.owner:
                raise AuthExpired('续登账号与当前账号不一致，请手动重新登录')
            self.store.set_secret(secret | result)
            self.state.update(connected=True, error='')
            self.store.log('学校登录已自动续期')

    async def sync(self):
        async with self.lock:
            self.state['busy'] = True
            try:
                api = self.api()
                courses = await api.courses()
                starts={}
                for c in courses:
                    sem=c.get('semester') or ''
                    label=(sem.get('name') or sem.get('title') or '') if isinstance(sem,dict) else str(sem)
                    start=c.get('start_date')
                    if label and isinstance(start,str) and start and (label not in starts or start<starts[label]):
                        starts[label]=start
                self.store.put('semester_starts:'+self.store.owner,starts)
                profile = self.store.get('profile', {})
                uid, username = str(profile.get('id', '')), profile.get('username', '')
                failures = 0
                for course in courses:
                    cid = str(course.get('id') or course.get('course_id') or '')
                    if not cid:
                        continue
                    name = str(course.get('name') or course.get('title') or course.get('course_title') or '未命名课程')
                    semester = course.get('semester') or course.get('academic_year') or ''
                    if isinstance(semester, dict):
                        semester = semester.get('name') or semester.get('title') or ''
                    self.store.save_course(cid, name, str(semester))
                    complete, error = True, ''
                    try:
                        history = await api.history(cid, uid)
                    except AuthExpired:
                        raise
                    except (ProtocolError, Rejected) as e:
                        history = getattr(e, 'partial', [])
                        complete, error = False, str(e)
                    for row in history:
                        rid = str(row.get('rollcall_id') or row.get('id') or '')
                        if not rid:
                            continue
                        status = status_of(row)
                        if status == 'unknown':
                            try:
                                detail = await api.detail(rid)
                                status = own_status(detail, uid, username)
                                if detail is None or status == 'unknown':
                                    complete, error = False, '部分签到无法核实本人状态'
                            except AuthExpired:
                                raise
                            except (ProtocolError, Rejected):
                                complete, error = False, '部分签到详情读取失败，已保留已有记录'
                        if status == 'absent' and row.get('is_expired') is False and not expired(row):
                            status = 'pending'
                        self.store.save_record(rid, cid, kind(row), status, timestamp(row))
                    self.store.sync_state(cid, complete, error)
                    failures += int(not complete)
                    # Yield so shutdown and page requests are not starved on large histories.
                    await asyncio.sleep(0)
                now = time.time()
                self.state.update(last_sync=now, connected=True,
                                  error=f'{failures} 门课程同步不完整，可打开课程查看原因' if failures else '')
                self.store.put('last_sync', now)
                self.store.log(f'历史同步完成：{len(courses)} 门课程，{failures} 门需核实', 'warning' if failures else 'info')
            finally:
                self.state['busy'] = False

    async def poll(self):
        async with self.lock:
            api = self.api()
            active = await api.active()
            profile = self.store.get('profile', {})
            uid, username = str(profile.get('id', '')), profile.get('username', '')
            settings = self.store.settings()
            display = []
            for event in active:
                rid = str(event.get('rollcall_id') or event.get('id') or '')
                if not rid or expired(event):
                    continue
                cid = str(event.get('course_id') or (event.get('course') or {}).get('id') or '')
                if not cid:
                    continue
                title = str(event.get('course_title') or event.get('course_name') or '未命名课程')
                self.store.save_course(cid, title)
                event_kind = kind(event)
                detail = await api.detail(rid)
                personal = own_status(detail, uid, username)
                if personal == 'unknown':
                    personal = status_of(event)
                current_status = 'pending' if personal == 'absent' else personal
                self.store.save_record(rid, cid, event_kind, current_status, timestamp(event))
                view = {'id': rid, 'course_id': cid, 'course': title, 'kind': event_kind, 'status': current_status, 'result': ''}
                display.append(view)
                attempt = self.store.attempt(rid)
                if personal in ('present', 'late', 'leave'):
                    if attempt and personal in ('present', 'late'):
                        self.store.finish(rid, 'confirmed')
                    continue
                if attempt:
                    view['result'] = attempt['outcome']
                    continue
                if not settings['enabled']:
                    view['result'] = '监控已暂停'
                    continue
                if event_kind not in ('number', 'radar'):
                    view['result'] = '请在课堂扫码完成签到'
                    continue
                if not settings['auto_'+event_kind]:
                    view['result'] = '仅提醒'
                    continue
                threshold=settings['wait_for_count']
                count=signed_count(detail,uid,username)
                view.update(signed_count=count,wait_for_count=threshold)
                if threshold>0 and (count is None or count<threshold):
                    view['result']='无法确认签到人数，继续等待' if count is None else f'等待其他同学签到：{count}/{threshold} 人'
                    continue
                code = ''
                if event_kind == 'number':
                    code = await api.number_code(event, detail)
                    if not code:
                        view['result'] = '等待平台提供签到码'
                        continue
                if settings['radar_mode'] == 'fixed' and event_kind == 'radar' and (settings['latitude'] is None or settings['longitude'] is None):
                    view['result'] = '请先填写签到位置'
                    continue
                # Claim is committed BEFORE the first network write; survives restart/timeouts.
                if not self.store.claim(rid):
                    continue
                outcome = 'unconfirmed'
                try:
                    if event_kind == 'number':
                        await api.answer_number(rid, code, self.store.get('device_id'))
                    else:
                        await self.radar(api, rid, settings)
                    for delay in (0, 1, 2):
                        if delay:
                            await asyncio.sleep(delay)
                        verified = own_status(await api.detail(rid), uid, username)
                        if verified in ('present', 'late'):
                            self.store.save_record(rid, cid, event_kind, verified, timestamp(event))
                            view['status'] = verified
                            outcome = 'confirmed'
                            break
                    view['result'] = outcome
                except AuthExpired:
                    self.store.finish(rid, 'unconfirmed')
                    raise
                except Rejected:
                    outcome = 'rejected'
                    view['result'] = outcome
                except ProtocolError:
                    view['result'] = 'unconfirmed'
                finally:
                    self.store.finish(rid, outcome)
                self.store.log(f'{title}：' + {'confirmed':'已回查确认签到', 'unconfirmed':'提交结果待核实，暂不重复提交', 'rejected':'平台拒绝提交'}[outcome], 'info' if outcome == 'confirmed' else 'warning')
            self.state.update(active=display, last_poll=time.time(), connected=True)

    async def radar(self, api, rid, settings):
        device = self.store.get('device_id')
        if settings['radar_mode'] == 'fixed':
            result = await api.answer_radar(rid, (settings['latitude'], settings['longitude']), device)
            if not result.get('accepted'):
                raise Rejected('平台未接受指定位置')
            return
        a, b = (24.3, 118.0), (24.6, 118.2)
        first = await api.answer_radar(rid, a, device)
        if first.get('accepted'):
            return
        second = await api.answer_radar(rid, b, device)
        if second.get('accepted'):
            return
        ra, rb = first.get('distance'), second.get('distance')
        if not isinstance(ra, (int, float)) or not isinstance(rb, (int, float)):
            raise Rejected('平台未提供可用距离')
        for point in candidates(a, b, ra, rb):
            if (await api.answer_radar(rid, point, device)).get('accepted'):
                return
        raise Rejected('本次雷达签到未被平台接受')

    async def worker(self):
        failures = 0
        while not self.stop.is_set():
            delay = self.store.settings()['poll_interval']
            try:
                if self.store.secret().get('cookie'):
                    if self.sync_requested.is_set() or not self.state['last_sync'] or time.time()-self.state['last_sync'] > self.store.settings()['history_interval']:
                        self.sync_requested.clear()
                        await self.sync()
                    if self.store.settings()['enabled']:
                        await self.poll()
                    failures = 0
                self.state['error'] = self.state['error'] if self.state.get('busy') else self.state.get('error','')
            except AuthExpired:
                self.state.update(connected=False, error='学校登录已失效，正在尝试续登')
                try:
                    await self.renew()
                    self.sync_requested.set()
                except Exception:
                    self.state.update(connected=False, error='学校登录失效，请在账号设置中重新登录')
                    failures += 1
            except Exception:
                self.state['error'] = '本轮连接或同步失败，将自动退避重试'
                failures += 1
                self.store.log(self.state['error'], 'warning')
            if failures:
                delay = min(900, delay * 2**min(failures, 5))
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def close(self):
        self.stop.set()
        await self.http.aclose()

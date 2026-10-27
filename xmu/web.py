from collections import defaultdict
from contextlib import asynccontextmanager
import asyncio
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import time
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response, Depends
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .platform import AuthExpired, ProtocolError, Rejected
from .service import Service
from .store import Store
from .semesters import group_courses

STATIC = Path(__file__).parent/'static'


class Unlock(BaseModel):
    password: str = Field(max_length=500)


class Account(BaseModel):
    username: str = Field(default='', max_length=100)
    password: str = Field(default='', max_length=500)
    cookie: str = Field(default='', max_length=16000)


class Settings(BaseModel):
    enabled: bool | None = None
    auto_number: bool | None = None
    auto_radar: bool | None = None
    poll_interval: int | None = Field(default=None, ge=15, le=600)
    history_interval: int | None = Field(default=None, ge=600, le=86400)
    wait_for_count: int | None = Field(default=None, ge=0, le=5000)
    radar_mode: Literal['fixed', 'adaptive'] | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


def create_app(data_dir=None, admin_password=None, start_worker=True):
    store = Store(data_dir or os.getenv('XMU_DATA_DIR', 'data'))
    admin_password = admin_password or os.getenv('XMU_ADMIN_PASSWORD')
    if not store.get('admin_hash'):
        password = admin_password or secrets.token_urlsafe(24)
        salt = secrets.token_hex(16)
        hashed = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 200000).hex()
        store.put('admin_hash', {'salt': salt, 'hash': hashed})
        if not admin_password:
            fd = os.open(store.directory/'admin-password.txt', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf8') as f:
                f.write(password+'\n')
    sessions = {}
    failures = defaultdict(list)
    service = Service(store)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(service.worker()) if start_worker else None
        yield
        service.stop.set()
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await service.close()
        store.close()

    app = FastAPI(title='厦大课程考勤簿', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan,
                  root_path=os.getenv('XMU_ROOT_PATH', ''))
    app.state.store, app.state.service = store, service

    @app.middleware('http')
    async def guard(request, call_next):
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('origin')
            if origin and urlsplit(origin).netloc != request.headers.get('host'):
                from fastapi.responses import JSONResponse
                return JSONResponse({'detail':'拒绝跨站操作'}, status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    def authorized(request: Request):
        token = request.cookies.get('xmu_session', '')
        if sessions.get(token, 0) < time.time():
            sessions.pop(token, None)
            raise HTTPException(401, '请先输入管理密码')

    @app.get('/')
    def index():
        return FileResponse(STATIC/'index.html')

    @app.get('/healthz')
    def health():
        return {'status': 'ok'}

    @app.post('/api/unlock')
    def unlock(body: Unlock, response: Response, request: Request):
        ip = request.client.host if request.client else 'unknown'
        failures[ip] = [t for t in failures[ip] if t > time.time()-300]
        if len(failures[ip]) >= 8:
            raise HTTPException(429, '尝试次数过多，请五分钟后重试')
        saved = store.get('admin_hash')
        digest = hashlib.pbkdf2_hmac('sha256', body.password.encode(), saved['salt'].encode(), 200000).hex()
        if not hmac.compare_digest(digest, saved['hash']):
            failures[ip].append(time.time())
            raise HTTPException(401, '管理密码不正确')
        failures.pop(ip, None)
        for key in list(sessions):
            if sessions[key] < time.time():
                sessions.pop(key)
        token = secrets.token_urlsafe(32)
        sessions[token] = time.time()+86400
        path = (os.getenv('XMU_ROOT_PATH', '').rstrip('/') or '')+'/'
        response.set_cookie('xmu_session', token, max_age=86400, httponly=True,
                            secure=request.url.scheme == 'https', samesite='strict', path=path)
        return {'ok': True}

    @app.post('/api/logout', dependencies=[Depends(authorized)])
    def logout(response: Response, request: Request):
        sessions.pop(request.cookies.get('xmu_session', ''), None)
        response.delete_cookie('xmu_session', path=(os.getenv('XMU_ROOT_PATH','').rstrip('/') or '')+'/')
        return {'ok': True}

    @app.get('/api/dashboard', dependencies=[Depends(authorized)])
    def dashboard():
        courses=store.courses()
        return {'profile': store.get('profile', {}), 'settings': store.settings(),
                'state': service.state, 'courses': courses, 'logs': store.logs(),
                'semester_groups':group_courses(courses,semester_starts=store.get('semester_starts:'+store.owner,{})),
                'has_credentials': bool(store.secret().get('cookie'))}

    @app.get('/api/courses/{course_id}', dependencies=[Depends(authorized)])
    def course(course_id: str):
        result = next((c for c in store.courses() if c['id'] == course_id), None)
        if result is None:
            raise HTTPException(404, '没有找到这门课程')
        return {'course': result, 'records': store.records(course_id)}

    @app.post('/api/account', dependencies=[Depends(authorized)])
    async def account(body: Account):
        if not body.cookie.strip() and (not body.username.strip() or not body.password):
            raise HTTPException(422, '请输入学校账号密码，或导入课程平台 Cookie')
        if '\r' in body.cookie or '\n' in body.cookie:
            raise HTTPException(422, 'Cookie 应为单行请求头内容')
        try:
            profile = await service.connect(**body.model_dump())
        except (AuthExpired, ProtocolError, Rejected) as e:
            raise HTTPException(400, str(e)) from None
        return {'ok': True, 'profile': profile}

    @app.put('/api/settings', dependencies=[Depends(authorized)])
    def settings(body: Settings):
        changes = body.model_dump(exclude_unset=True)
        # Reject null for switches/intervals while allowing empty fixed coordinates.
        if any(v is None and k not in ('latitude', 'longitude') for k,v in changes.items()):
            raise HTTPException(422, '设置值不能为空')
        if changes.get('enabled') and not store.secret().get('cookie'):
            raise HTTPException(409, '请先登录学校账号，再启动监控')
        previous = store.settings()
        result = store.update_settings(changes)
        if previous['enabled'] != result['enabled']:
            store.log('已启动自动签到监控' if result['enabled'] else '已暂停自动签到监控')
        return result

    @app.post('/api/sync', dependencies=[Depends(authorized)])
    def sync():
        if not store.secret().get('cookie'):
            raise HTTPException(409, '请先登录学校账号')
        if service.state['busy']:
            return {'ok': True, 'message': '正在同步，请稍候'}
        service.sync_requested.set()
        return {'ok': True, 'message': '已安排同步，后台将读取各课程历史'}

    app.mount('/static', StaticFiles(directory=STATIC), name='static')
    return app

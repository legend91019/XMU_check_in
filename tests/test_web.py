from fastapi.testclient import TestClient

from xmu.web import create_app


def client(tmp_path):
    return TestClient(create_app(tmp_path, admin_password='a-test-admin-password', start_worker=False))


def unlock(c):
    r = c.post('/api/unlock', json={'password': 'a-test-admin-password'})
    assert r.status_code == 200


def test_dashboard_requires_login(tmp_path):
    with client(tmp_path) as c:
        assert c.get('/').status_code == 200
        assert c.get('/api/dashboard').status_code == 401
        assert c.post('/api/unlock', json={'password':'incorrect'}).status_code == 401
        unlock(c)
        assert c.get('/api/dashboard').status_code == 200
        assert c.cookies.get('xmu_session')


def test_settings_never_return_secrets(tmp_path):
    app = create_app(tmp_path, admin_password='a-test-admin-password', start_worker=False)
    app.state.store.set_secret({'password': 'school-password', 'cookie': 'session=hidden'})
    with TestClient(app) as c:
        unlock(c)
        text = c.get('/api/dashboard').text
        assert 'school-password' not in text
        assert 'session=hidden' not in text
        assert 'secret.key' not in text


def test_settings_validation_and_logout(tmp_path):
    with client(tmp_path) as c:
        unlock(c)
        assert c.put('/api/settings', json={'poll_interval': 0}).status_code == 422
        assert c.put('/api/settings', json={'poll_interval': 30, 'enabled': False}).status_code == 200
        assert c.post('/api/logout').status_code == 200
        assert c.get('/api/dashboard').status_code == 401


def test_cross_origin_mutation_rejected(tmp_path):
    with client(tmp_path) as c:
        unlock(c)
        assert c.put('/api/settings', json={'enabled': True}, headers={'Origin':'https://evil.example'}).status_code == 403


def test_missing_school_login_is_actionable(tmp_path):
    with client(tmp_path) as c:
        unlock(c)
        assert c.post('/api/sync').status_code == 409
        assert c.put('/api/settings', json={'enabled': True}).status_code == 409


def test_course_details_are_account_scoped(tmp_path):
    app = create_app(tmp_path, admin_password='a-test-admin-password', start_worker=False)
    store = app.state.store
    store.set_profile({'id':'42'})
    store.save_course('7', '电磁学')
    store.save_record('10','7','number','absent','2026-10-20T08:00:00+08:00')
    with TestClient(app) as c:
        unlock(c)
        data = c.get('/api/courses/7').json()
        assert data['records'][0]['status'] == 'absent'
        assert c.get('/api/courses/unknown').status_code == 404


def test_static_assets_with_https_path_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv('XMU_ROOT_PATH','/attendance')
    with client(tmp_path) as c:
        for path in ('/attendance/','/attendance/static/app.js','/attendance/static/style.css'):
            assert c.get(path).status_code == 200


def test_wait_threshold_setting_and_semester_groups(tmp_path):
    app=create_app(tmp_path,admin_password='a-test-admin-password',start_worker=False)
    store=app.state.store
    store.set_profile({'id':'42'})
    store.save_course('new','当前课程','2026-2027第1学期')
    store.save_course('old','旧课程','2025-2026第2学期')
    with TestClient(app) as c:
        unlock(c)
        assert c.put('/api/settings',json={'wait_for_count':-1}).status_code==422
        assert c.put('/api/settings',json={'wait_for_count':10}).status_code==200
        d=c.get('/api/dashboard').json()
        assert d['settings']['wait_for_count']==10
        assert len(d['semester_groups'])==2
        assert sum(len(g['courses']) for g in d['semester_groups'])==2

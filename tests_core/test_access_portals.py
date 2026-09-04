from contextlib import contextmanager

import pytest
from fastapi import HTTPException, Response
from pydantic import ValidationError

from api import session_store
from api.routers.auth import login
from api.schemas import LoginRequest
from services import db, onboarding, tenancy
from tests_core.test_saas_onboarding_v320 import signup_database
from tests_core.test_postgresql_tenant_boundaries import postgres_database


@pytest.fixture
def portal_database(signup_database, monkeypatch):
    monkeypatch.setattr(session_store, 'connect', db.connect)
    monkeypatch.setattr(session_store, '_SCHEMA_READY', False)
    session_store.ensure_api_session_schema()
    yield signup_database


def register(kind='merchant', suffix='one'):
    return onboarding.register_merchant(company_name=f'Company {suffix}', username=f'user-{suffix}',
        password='Synthetic-password-123', tenant_type=kind, plan_code='enterprise')


def enter(area, suffix='one', password='Synthetic-password-123'):
    response = Response()
    result = login(LoginRequest(username=f'user-{suffix}', password=password, area=area), response)
    return result, response


@pytest.mark.parametrize('area,kind', [('seller','merchant'), ('agency','agency')])
def test_matching_portal_issues_real_scoped_session(portal_database, area, kind):
    registered = register(kind)
    result, response = enter(area)
    assert result.user.active_tenant_id == registered['tenant']['id']
    assert result.user.active_tenant_type == kind
    assert not result.user.is_admin
    assert session_store.session_user(result.token)['active_tenant_id'] == registered['tenant']['id']
    assert 'HttpOnly' in response.headers['set-cookie']


@pytest.mark.parametrize('area,kind', [('agency','merchant'), ('seller','agency'), ('admin','merchant'), ('admin','agency')])
def test_wrong_portal_rejected_without_session_or_cookie(portal_database, area, kind):
    register(kind)
    response = Response()
    with pytest.raises(HTTPException) as exc:
        login(LoginRequest(username='user-one', password='Synthetic-password-123', area=area), response)
    assert exc.value.status_code == 403
    assert db.rows('SELECT * FROM api_sessions') == []
    assert 'set-cookie' not in response.headers


def test_admin_login_requires_global_flag_not_owner_role(portal_database):
    registered = register()
    db.execute('UPDATE app_users SET is_admin=1 WHERE id=?', (registered['user_id'],))
    result, _ = enter('admin')
    assert result.user.is_admin


def test_portal_selects_authorized_type_instead_of_legacy_agency_default(portal_database):
    registered = register('agency')
    seller = register('merchant', 'seller')
    tenancy.add_membership(seller['tenant']['id'], registered['user_id'], role='viewer')
    assert tenancy.default_tenant_id(registered['user_id']) == registered['tenant']['id']
    result, _ = enter('seller')
    assert result.user.active_tenant_id == seller['tenant']['id']
    assert result.user.tenant_role == 'viewer'
    result, _ = enter('agency')
    assert result.user.active_tenant_id == registered['tenant']['id']


def test_explicit_session_target_cannot_escape_memberships(portal_database):
    registered = register()
    other = register(suffix='other')
    with pytest.raises(ValueError, match='non autorizzato'):
        session_store.issue_session(registered['user_id'], tenant_id=other['tenant']['id'])
    assert db.rows('SELECT * FROM api_sessions') == []


def test_wrong_password_and_invalid_area_never_create_session(portal_database):
    register()
    with pytest.raises(HTTPException) as exc:
        enter('seller', password='wrong-password')
    assert exc.value.status_code == 401
    with pytest.raises(ValidationError):
        LoginRequest(username='user-one', password='Synthetic-password-123', area='superadmin')
    assert db.rows('SELECT * FROM api_sessions') == []


def test_postgresql_agency_clients_are_sorted_and_deduplicated(postgres_database, monkeypatch):
    from services.postgresql_backend import PostgreSQLCompatConnection
    @contextmanager
    def connect():
        with postgres_database() as raw:
            yield PostgreSQLCompatConnection(raw)
    monkeypatch.setattr(db, 'connect', connect)
    monkeypatch.setattr(tenancy, 'ensure_tenancy_schema', lambda: None)
    with connect() as con:
        con.executescript('''
            CREATE TABLE tenants(id INTEGER PRIMARY KEY,name TEXT,slug TEXT,tenant_type TEXT,status TEXT,plan_code TEXT);
            CREATE TABLE tenant_memberships(tenant_id INTEGER,user_id INTEGER,role TEXT,active INTEGER);
            CREATE TABLE agency_clients(agency_tenant_id INTEGER,client_tenant_id INTEGER,active INTEGER);
            INSERT INTO tenants VALUES (1,'Agency A','a','agency','active','enterprise'),
                (2,'Agency B','b','agency','active','enterprise'),(3,'Client','c','merchant','active','enterprise'),
                (4,'Unassigned','d','merchant','active','enterprise');
            INSERT INTO tenant_memberships VALUES (1,99,'viewer',1),(2,99,'manager',1);
            INSERT INTO agency_clients VALUES (1,3,1),(2,3,1);
        ''')
    items=tenancy.accessible_tenants_for_user(99)
    assert [x['id'] for x in items]==[1,2,3]
    assert items[-1]['role']=='manager'

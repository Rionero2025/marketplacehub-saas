import io

import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from services.object_storage import S3ObjectStorage, StorageConfig, sha256_bytes
from tools import storage_probe


@pytest.fixture
def s3(tmp_path):
    config=StorageConfig('s3','test-bucket','test','', 'us-east-1', 'testing', 'testing', tmp_path)
    store=S3ObjectStorage(config)
    return store,config


def test_s3_distinguishes_missing_object_from_denied_access(s3):
    store,_=s3
    params={'Bucket':'test-bucket','Key':'test/missing'}
    with Stubber(store.client) as stub:
        stub.add_client_error('head_object',service_error_code='404',http_status_code=404,expected_params=params)
        stub.add_client_error('head_object',service_error_code='AccessDenied',http_status_code=403,expected_params=params)
        assert store.exists('missing') is False
        with pytest.raises(ClientError):
            store.exists('missing')
        stub.assert_no_pending_responses()


def test_s3_delete_reports_failure(s3):
    store,_=s3
    with Stubber(store.client) as stub:
        stub.add_client_error('delete_object',service_error_code='AccessDenied',http_status_code=403,
                              expected_params={'Bucket':'test-bucket','Key':'test/object'})
        with pytest.raises(ClientError):
            store.delete('object')


def test_probe_restores_and_verifies_cleanup_through_s3_adapter(s3,monkeypatch):
    store,config=s3
    payload=b'probe-content'
    params={'Bucket':'test-bucket','Key':'test/_probes/test.bin'}
    monkeypatch.setattr(storage_probe,'object_store',lambda:store)
    monkeypatch.setattr(storage_probe,'storage_config',lambda:config)
    with Stubber(store.client) as stub:
        stub.add_response('get_object',{'Body':io.BytesIO(payload)},params)
        stub.add_response('delete_object',{},params)
        stub.add_client_error('head_object',service_error_code='404',http_status_code=404,expected_params=params)
        assert storage_probe.restore_probe('_probes/test.bin',sha256_bytes(payload),cleanup=True)=={
            'verified':True,'size_bytes':len(payload),'cleanup':True}
        stub.assert_no_pending_responses()


def test_probe_does_not_delete_after_integrity_failure(s3,monkeypatch):
    store,config=s3
    monkeypatch.setattr(storage_probe,'object_store',lambda:store)
    monkeypatch.setattr(storage_probe,'storage_config',lambda:config)
    with Stubber(store.client) as stub:
        stub.add_response('get_object',{'Body':io.BytesIO(b'corrupt')},{'Bucket':'test-bucket','Key':'test/_probes/test.bin'})
        with pytest.raises(ValueError,match='SHA-256'):
            storage_probe.restore_probe('_probes/test.bin','0'*64,cleanup=True)
        stub.assert_no_pending_responses()

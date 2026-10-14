from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from storage3.exceptions import StorageApiError

import app.services.storage_service as storage_service


class FakeBucket:
    def __init__(self, bucket_id: str) -> None:
        self.id = bucket_id
        self.name = bucket_id


class FakeStorageAPI:
    def __init__(self, buckets: list[str] | None = None) -> None:
        self.buckets = {bucket: FakeBucket(bucket) for bucket in (buckets or [])}
        self.created: list[str] = []
        self.uploads: list[tuple[str, str, bytes]] = []

    def list_buckets(self) -> list[FakeBucket]:
        return list(self.buckets.values())

    def create_bucket(
        self,
        bucket_id: str,
        name: str | None = None,
        options: dict[str, object] | None = None,
    ) -> dict[str, str]:
        del name, options
        if bucket_id in self.buckets:
            raise StorageApiError('Bucket already exists', 'BucketAlreadyExists', 400)
        self.buckets[bucket_id] = FakeBucket(bucket_id)
        self.created.append(bucket_id)
        return {'name': bucket_id}

    def from_(self, bucket: str) -> SimpleNamespace:
        storage = self

        class BucketProxy:
            def upload(self, path: str, contents: bytes, file_options: dict[str, str]) -> None:
                del file_options
                if bucket not in storage.buckets:
                    raise StorageApiError('Bucket not found', 'NoSuchBucket', 404)
                storage.uploads.append((bucket, path, contents))

        return BucketProxy()


@pytest.fixture(autouse=True)
def reset_ensured_buckets() -> None:
    storage_service._ensured_buckets.clear()
    storage_service.get_storage_client.cache_clear()


def test_upload_file_creates_missing_bucket(monkeypatch) -> None:
    fake_storage = FakeStorageAPI(buckets=['policy-documents'])

    class FakeSupabaseClient:
        storage = fake_storage

    monkeypatch.setattr(storage_service, 'get_storage_client', lambda: FakeSupabaseClient())

    stored = storage_service.upload_file(
        'compliance-reports',
        'org_example/compliance-reports/report-id/report.docx',
        b'docx-bytes',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    )

    assert 'compliance-reports' in fake_storage.created
    assert stored.bucket == 'compliance-reports'
    assert fake_storage.uploads[0][0] == 'compliance-reports'


def test_parse_storage_uri_accepts_supabase_scheme() -> None:
    stored = storage_service.parse_storage_uri(
        'supabase://policy-redlines/org_example/rev-1/redline.docx'
    )
    assert stored.bucket == 'policy-redlines'
    assert stored.path == 'org_example/rev-1/redline.docx'


def test_create_download_url_retries_without_download_flag(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class BucketProxy:
        def create_signed_url(
            self,
            path: str,
            expires_in: int,
            options: dict[str, object] | None = None,
        ) -> dict[str, str]:
            calls.append({'path': path, 'expires_in': expires_in, 'options': options or {}})
            if options and options.get('download'):
                raise StorageApiError('Invalid download option', 'InvalidRequest', 400)
            return {'signedURL': 'https://signed.example/redline.docx'}

    class FakeSupabaseClient:
        def from_(self, bucket: str) -> BucketProxy:
            del bucket
            return BucketProxy()

    class FakeStorageAPI:
        def from_(self, bucket: str) -> BucketProxy:
            del bucket
            return BucketProxy()

    monkeypatch.setattr(storage_service, 'ensure_storage_bucket', lambda _bucket: None)
    monkeypatch.setattr(
        storage_service,
        'get_storage_client',
        lambda: type('Client', (), {'storage': FakeStorageAPI()})(),
    )

    url = storage_service.create_download_url(
        'storage://policy-redlines/org_example/rev-1/redline.docx'
    )

    assert url == 'https://signed.example/redline.docx'
    assert len(calls) == 2
    assert calls[0]['options'] == {'download': True}
    assert calls[1]['options'] == {}


def test_download_file_falls_back_to_signed_url(monkeypatch) -> None:
    class BucketProxy:
        def download(self, path: str) -> bytes:
            del path
            raise StorageApiError('Invalid request', 'InvalidRequest', 400)

        def create_signed_url(
            self,
            path: str,
            expires_in: int,
            options: dict[str, object] | None = None,
        ) -> dict[str, str]:
            del path, expires_in, options
            return {'signedURL': 'https://signed.example/redline.docx'}

    class FakeStorageAPI:
        def from_(self, bucket: str) -> BucketProxy:
            del bucket
            return BucketProxy()

    class FakeResponse:
        content = b'docx-bytes'

        @staticmethod
        def raise_for_status() -> None:
            return None

    monkeypatch.setattr(
        storage_service, 'ensure_storage_bucket', lambda _bucket, **_kwargs: None)
    monkeypatch.setattr(
        storage_service,
        'get_storage_client',
        lambda: type('Client', (), {'storage': FakeStorageAPI()})(),
    )
    monkeypatch.setattr(storage_service.httpx, 'get', lambda *_args, **_kwargs: FakeResponse())

    content = storage_service.download_file(
        'storage://policy-redlines/org_example/rev-1/redline.docx'
    )

    assert content == b'docx-bytes'


def test_upload_file_logs_storage_upload_error(monkeypatch) -> None:
    fake_storage = FakeStorageAPI(buckets=['compliance-reports'])

    class FailingBucketProxy:
        def upload(self, path: str, contents: bytes, file_options: dict[str, str]) -> None:
            del path, contents, file_options
            raise StorageApiError('Invalid object name', 'InvalidKey', 400)

    class FakeSupabaseClient:
        storage = fake_storage

    def fake_from_(bucket: str) -> FailingBucketProxy:
        del bucket
        return FailingBucketProxy()

    fake_storage.from_ = fake_from_  # type: ignore[method-assign]
    monkeypatch.setattr(storage_service, 'get_storage_client', lambda: FakeSupabaseClient())

    with pytest.raises(HTTPException) as exc_info:
        storage_service.upload_file('compliance-reports', 'path/file.docx', b'bytes', None)

    assert exc_info.value.status_code == 502

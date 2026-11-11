"""Local or private Supabase Storage for user-uploaded files."""

import mimetypes
import os
import threading
from urllib.parse import quote

import requests


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
BUCKET_ENV = {
    "requests": "SUPABASE_STORAGE_BUCKET_REQUESTS",
    "complaints": "SUPABASE_STORAGE_BUCKET_COMPLAINTS",
    "profiles": "SUPABASE_STORAGE_BUCKET_PROFILES",
}
DEFAULT_BUCKETS = {
    "requests": "barangayconnect-requests",
    "complaints": "barangayconnect-complaints",
    "profiles": "barangayconnect-profiles",
}
_PRIVATE_BUCKETS = set()
_BUCKET_LOCK = threading.Lock()


class StorageError(RuntimeError):
    pass


class StorageNotFound(StorageError):
    pass


def get_backend():
    backend = os.environ.get("STORAGE_BACKEND", "local").strip().lower()
    if backend not in {"local", "supabase"}:
        raise ValueError("STORAGE_BACKEND must be either 'local' or 'supabase'.")
    return backend


def _local_path(subdir, stored_name, upload_root=None):
    if subdir not in BUCKET_ENV or os.path.basename(stored_name) != stored_name:
        raise ValueError("Invalid storage path.")
    root = os.path.abspath(os.path.join(upload_root or UPLOAD_DIR, subdir))
    path = os.path.abspath(os.path.join(root, stored_name))
    if os.path.commonpath([root, path]) != root:
        raise ValueError("Invalid storage path.")
    return path


def _supabase_object_url(subdir, stored_name):
    if subdir not in BUCKET_ENV or os.path.basename(stored_name) != stored_name:
        raise ValueError("Invalid storage path.")
    base_url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not base_url or not service_key:
        raise StorageError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required for Supabase Storage."
        )
    bucket = os.environ.get(BUCKET_ENV[subdir], DEFAULT_BUCKETS[subdir]).strip()
    if not bucket:
        raise StorageError("A Supabase Storage bucket is not configured.")
    headers = {"apikey": service_key, "Authorization": f"Bearer {service_key}"}
    _require_private_bucket(base_url, bucket, headers)
    url = f"{base_url}/storage/v1/object/{quote(bucket, safe='')}/{quote(stored_name, safe='')}"
    return url, headers


def _require_private_bucket(base_url, bucket, headers):
    with _BUCKET_LOCK:
        cache_key = (base_url, bucket)
        if cache_key in _PRIVATE_BUCKETS:
            return
        url = f"{base_url}/storage/v1/bucket/{quote(bucket, safe='')}"
        try:
            response = requests.get(url, headers=headers, timeout=(10, 30))
        except requests.RequestException as error:
            raise StorageError(
                "Supabase Storage bucket configuration could not be verified."
            ) from error
        if response.status_code == 404:
            raise StorageError(
                f"Required private Supabase Storage bucket {bucket!r} does not exist."
            )
        if response.status_code != 200:
            raise StorageError(
                f"Supabase Storage bucket verification failed (HTTP {response.status_code})."
            )
        try:
            is_public = response.json().get("public")
        except (AttributeError, ValueError) as error:
            raise StorageError("Supabase Storage returned invalid bucket metadata.") from error
        if is_public is not False:
            raise StorageError(
                f"Supabase Storage bucket {bucket!r} must be private."
            )
        _PRIVATE_BUCKETS.add(cache_key)


def upload(subdir, stored_name, stream, upload_root=None):
    if get_backend() == "local":
        path = _local_path(subdir, stored_name, upload_root)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        stream.seek(0)
        stream.save(path) if hasattr(stream, "save") else _write_stream(stream, path)
        return

    url, headers = _supabase_object_url(subdir, stored_name)
    content_type = mimetypes.guess_type(stored_name)[0] or "application/octet-stream"
    headers = {**headers, "Content-Type": content_type, "x-upsert": "false"}
    stream.seek(0)
    try:
        response = requests.post(url, headers=headers, data=stream, timeout=(10, 60))
    except requests.RequestException as error:
        raise StorageError("Supabase Storage upload could not be completed.") from error
    if response.status_code not in {200, 201}:
        raise StorageError(
            f"Supabase Storage upload failed (HTTP {response.status_code})."
        )


def _write_stream(stream, path):
    with open(path, "wb") as target:
        while chunk := stream.read(1024 * 1024):
            target.write(chunk)


def download(subdir, stored_name, upload_root=None):
    if get_backend() == "local":
        path = _local_path(subdir, stored_name, upload_root)
        try:
            with open(path, "rb") as source:
                return source.read()
        except FileNotFoundError as error:
            raise StorageNotFound("The stored file was not found.") from error
        except OSError as error:
            raise StorageError("The stored file could not be read.") from error

    url, headers = _supabase_object_url(subdir, stored_name)
    try:
        response = requests.get(url, headers=headers, timeout=(10, 60))
    except requests.RequestException as error:
        raise StorageError("Supabase Storage download could not be completed.") from error
    if response.status_code == 404:
        raise StorageNotFound("The stored file was not found.")
    if response.status_code != 200:
        raise StorageError(
            f"Supabase Storage download failed (HTTP {response.status_code})."
        )
    return response.content


def file_size(subdir, stored_name, upload_root=None):
    if get_backend() == "local":
        path = _local_path(subdir, stored_name, upload_root)
        try:
            return os.path.getsize(path)
        except FileNotFoundError as error:
            raise StorageNotFound("The stored file was not found.") from error
        except OSError as error:
            raise StorageError("The stored file size could not be read.") from error

    url, headers = _supabase_object_url(subdir, stored_name)
    try:
        response = requests.head(url, headers=headers, timeout=(10, 30))
    except requests.RequestException as error:
        raise StorageError("Supabase Storage metadata could not be read.") from error
    if response.status_code == 404:
        raise StorageNotFound("The stored file was not found.")
    if response.status_code != 200:
        raise StorageError(
            f"Supabase Storage metadata lookup failed (HTTP {response.status_code})."
        )
    try:
        return int(response.headers["Content-Length"])
    except (KeyError, ValueError) as error:
        raise StorageError("Supabase Storage did not return a valid content length.") from error


def delete(subdir, stored_name):
    if get_backend() == "local":
        path = _local_path(subdir, stored_name)
        try:
            os.remove(path)
        except FileNotFoundError:
            return
        except OSError as error:
            raise StorageError("The stored file could not be deleted.") from error
        return

    url, headers = _supabase_object_url(subdir, stored_name)
    try:
        response = requests.delete(url, headers=headers, timeout=(10, 30))
    except requests.RequestException as error:
        raise StorageError("Supabase Storage deletion could not be completed.") from error
    if response.status_code not in {200, 204, 404}:
        raise StorageError(
            f"Supabase Storage deletion failed (HTTP {response.status_code})."
        )

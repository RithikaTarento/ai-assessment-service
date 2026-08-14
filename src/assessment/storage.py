"""
Storage service abstraction for uploaded assessment files.
Supports local filesystem and GCP Cloud Storage.
Switch backends by setting DOCUMENT_STORAGE_TYPE=local|gcs in .env.
"""
import os
import uuid
import shutil
import logging
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Tuple
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class StorageType(str, Enum):
    LOCAL = "local"
    GCS = "gcs"


class StorageService(ABC):
    """Abstract base class for storage backends"""

    @abstractmethod
    def save_file(self, file_obj: BinaryIO, filename: str, job_id: str) -> Tuple[str, int]:
        """Save file and return (stored_path, file_size). stored_path is used for read/delete."""
        pass

    @abstractmethod
    def read_file(self, stored_path: str, local_dest: Path) -> Path:
        """Download/copy file to local_dest and return the local path."""
        pass

    @abstractmethod
    def delete_file(self, stored_path: str) -> bool:
        """Delete file. Returns True if successful."""
        pass

    @abstractmethod
    def file_exists(self, stored_path: str) -> bool:
        """Check if file exists at stored_path."""
        pass

    @abstractmethod
    def sync_directory_to_storage(self, local_dir: Path, storage_prefix: str) -> None:
        """Upload all files in local_dir recursively to the storage backend under storage_prefix."""
        pass

    @abstractmethod
    def sync_directory_from_storage(self, storage_prefix: str, local_dir: Path) -> bool:
        """Download all files from storage_prefix into local_dir. Returns True if any files were found."""
        pass


class LocalStorageService(StorageService):
    """Local filesystem storage — for single-node / Docker Compose deployments"""

    def __init__(self, root_path: str):
        self.root_path = Path(root_path)
        self.root_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"StorageService: LOCAL — root={self.root_path.resolve()}")

    def save_file(self, file_obj: BinaryIO, filename: str, job_id: str) -> Tuple[str, int]:
        subdir = self.root_path / "uploads" / job_id
        subdir.mkdir(parents=True, exist_ok=True)
        _, ext = os.path.splitext(filename)
        stored_filename = f"{uuid.uuid4()}{ext}"
        full_path = subdir / stored_filename
        size = 0
        with open(full_path, "wb") as f:
            while chunk := file_obj.read(8192):
                f.write(chunk)
                size += len(chunk)
        stored_path = str(full_path.relative_to(self.root_path))
        logger.info(f"Storage save (local): {filename} → {stored_path} ({size} bytes)")
        return stored_path, size

    def read_file(self, stored_path: str, local_dest: Path) -> Path:
        full_path = self.root_path / stored_path
        if not full_path.exists():
            raise FileNotFoundError(f"File not found: {stored_path}")
        local_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(full_path), str(local_dest))
        logger.info(f"Storage read (local): {stored_path} → {local_dest}")
        return local_dest

    def delete_file(self, stored_path: str) -> bool:
        try:
            full_path = self.root_path / stored_path
            if full_path.exists():
                full_path.unlink()
                logger.info(f"Storage delete (local): {stored_path}")
                return True
            return False
        except Exception as e:
            logger.warning(f"Storage delete (local) failed for {stored_path}: {e}")
            return False

    def file_exists(self, stored_path: str) -> bool:
        return (self.root_path / stored_path).exists()

    def sync_directory_to_storage(self, local_dir: Path, storage_prefix: str) -> None:
        dest = self.root_path / storage_prefix
        if local_dir.resolve() == dest.resolve():
            return
        if local_dir.exists():
            shutil.copytree(str(local_dir), str(dest), dirs_exist_ok=True)
            logger.info(f"sync_directory_to_storage (local): {local_dir} → {dest}")

    def sync_directory_from_storage(self, storage_prefix: str, local_dir: Path) -> bool:
        src = self.root_path / storage_prefix
        if not src.exists() or not any(src.rglob("*")):
            return False
        local_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(src), str(local_dir), dirs_exist_ok=True)
        logger.info(f"sync_directory_from_storage (local): {src} → {local_dir}")
        return True


class GCSStorageService(StorageService):
    """Google Cloud Storage backend — for multi-pod / Kubernetes deployments"""

    def __init__(self, bucket_name: str, prefix: str = "ai-assessments/uploads",
                 credentials_path: str = None):
        from google.cloud import storage as gcs
        self.bucket_name = bucket_name
        self.prefix = prefix.strip("/")
        if credentials_path and os.path.exists(credentials_path):
            self.client = gcs.Client.from_service_account_json(credentials_path)
            logger.info(f"StorageService: GCS — bucket={bucket_name} credentials={credentials_path}")
        else:
            self.client = gcs.Client()
            logger.info(f"StorageService: GCS — bucket={bucket_name} (default credentials)")
        self.bucket = self.client.bucket(bucket_name)

    def _blob_name(self, job_id: str, filename: str) -> str:
        return f"{self.prefix}/{job_id}/{filename}"

    def save_file(self, file_obj: BinaryIO, filename: str, job_id: str) -> Tuple[str, int]:
        _, ext = os.path.splitext(filename)
        stored_filename = f"{uuid.uuid4()}{ext}"
        blob_name = self._blob_name(job_id, stored_filename)
        blob = self.bucket.blob(blob_name)
        file_obj.seek(0)
        content = file_obj.read()
        blob.upload_from_string(content)
        logger.info(f"Storage save (GCS): {filename} → gs://{self.bucket_name}/{blob_name} ({len(content)} bytes)")
        return blob_name, len(content)

    def read_file(self, stored_path: str, local_dest: Path) -> Path:
        blob = self.bucket.blob(stored_path)
        if not blob.exists():
            raise FileNotFoundError(f"File not found in GCS: {stored_path}")
        local_dest.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(local_dest))
        logger.info(f"Storage read (GCS): gs://{self.bucket_name}/{stored_path} → {local_dest}")
        return local_dest

    def delete_file(self, stored_path: str) -> bool:
        try:
            blob = self.bucket.blob(stored_path)
            if blob.exists():
                blob.delete()
                logger.info(f"Storage delete (GCS): gs://{self.bucket_name}/{stored_path}")
                return True
            return False
        except Exception as e:
            logger.warning(f"Storage delete (GCS) failed for {stored_path}: {e}")
            return False

    def file_exists(self, stored_path: str) -> bool:
        return self.bucket.blob(stored_path).exists()

    def sync_directory_to_storage(self, local_dir: Path, storage_prefix: str) -> None:
        prefix = storage_prefix.strip("/")
        uploaded = 0
        for file_path in local_dir.rglob("*"):
            if not file_path.is_file():
                continue
            relative = file_path.relative_to(local_dir)
            blob_name = f"{prefix}/{relative.as_posix()}"
            self.bucket.blob(blob_name).upload_from_filename(str(file_path))
            uploaded += 1
        logger.info(f"sync_directory_to_storage (GCS): {local_dir} → gs://{self.bucket_name}/{prefix}/ ({uploaded} files)")

    def sync_directory_from_storage(self, storage_prefix: str, local_dir: Path) -> bool:
        prefix = storage_prefix.strip("/") + "/"
        blobs = list(self.client.list_blobs(self.bucket_name, prefix=prefix))
        if not blobs:
            return False
        local_dir.mkdir(parents=True, exist_ok=True)
        for blob in blobs:
            relative_name = blob.name[len(prefix):]
            if not relative_name:
                continue
            dest = local_dir / relative_name
            dest.parent.mkdir(parents=True, exist_ok=True)
            blob.download_to_filename(str(dest))
        logger.info(f"sync_directory_from_storage (GCS): gs://{self.bucket_name}/{prefix} → {local_dir} ({len(blobs)} files)")
        return True


_storage_service: StorageService = None


def get_storage_service() -> StorageService:
    """Factory — returns configured storage backend. Singleton per process."""
    global _storage_service
    if _storage_service is not None:
        return _storage_service

    from .config import (
        DOCUMENT_STORAGE_TYPE, INTERACTIVE_COURSES_PATH,
        GCS_BUCKET_NAME, GCS_UPLOAD_PREFIX, GCS_CREDENTIALS
    )

    if DOCUMENT_STORAGE_TYPE == StorageType.GCS:
        if not GCS_BUCKET_NAME:
            raise ValueError("GCS_BUCKET_NAME must be set when DOCUMENT_STORAGE_TYPE=gcs")
        _storage_service = GCSStorageService(
            bucket_name=GCS_BUCKET_NAME,
            prefix=GCS_UPLOAD_PREFIX,
            credentials_path=GCS_CREDENTIALS or None,
        )
    else:
        _storage_service = LocalStorageService(root_path=INTERACTIVE_COURSES_PATH)

    return _storage_service

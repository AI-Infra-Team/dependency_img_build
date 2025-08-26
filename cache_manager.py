import os
import subprocess
import json
import tempfile
import shutil
from typing import Optional, Dict, Any
from abc import ABC, abstractmethod
from config import CacheLevel, CacheConfig


class CacheBackend(ABC):
    @abstractmethod
    def exists(self, key: str) -> bool:
        """Check if cache entry exists"""
        pass
    
    @abstractmethod
    def get(self, key: str, local_path: str) -> bool:
        """Download cache entry to local path. Returns True if successful"""
        pass
    
    @abstractmethod
    def put(self, key: str, local_path: str) -> bool:
        """Upload local path to cache. Returns True if successful"""
        pass
    
    @abstractmethod
    def delete(self, key: str) -> bool:
        """Delete cache entry"""
        pass


class LocalCache(CacheBackend):
    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
    
    def _get_path(self, key: str) -> str:
        return os.path.join(self.cache_dir, f"{key}.tar.gz")
    
    def exists(self, key: str) -> bool:
        return os.path.exists(self._get_path(key))
    
    def get(self, key: str, local_path: str) -> bool:
        cache_path = self._get_path(key)
        if not os.path.exists(cache_path):
            return False
        
        try:
            shutil.copy2(cache_path, local_path)
            return True
        except Exception:
            return False
    
    def put(self, key: str, local_path: str) -> bool:
        cache_path = self._get_path(key)
        try:
            shutil.copy2(local_path, cache_path)
            return True
        except Exception:
            return False
    
    def delete(self, key: str) -> bool:
        cache_path = self._get_path(key)
        try:
            if os.path.exists(cache_path):
                os.remove(cache_path)
            return True
        except Exception:
            return False


class MinioCache(CacheBackend):
    def __init__(self, config: CacheConfig):
        self.endpoint = config.minio_endpoint
        self.bucket = config.minio_bucket
        self.access_key = config.minio_access_key
        self.secret_key = config.minio_secret_key
        
    def _run_mc_command(self, command: list) -> bool:
        """Run MinIO client command"""
        try:
            env = os.environ.copy()
            env.update({
                'MC_HOST_cache': f'http://{self.access_key}:{self.secret_key}@{self.endpoint}'
            })
            
            result = subprocess.run(
                ['mc'] + command,
                env=env,
                capture_output=True,
                text=True
            )
            return result.returncode == 0
        except Exception:
            return False
    
    def exists(self, key: str) -> bool:
        return self._run_mc_command(['stat', f'cache/{self.bucket}/{key}.tar.gz'])
    
    def get(self, key: str, local_path: str) -> bool:
        return self._run_mc_command([
            'cp', f'cache/{self.bucket}/{key}.tar.gz', local_path
        ])
    
    def put(self, key: str, local_path: str) -> bool:
        return self._run_mc_command([
            'cp', local_path, f'cache/{self.bucket}/{key}.tar.gz'
        ])
    
    def delete(self, key: str) -> bool:
        return self._run_mc_command(['rm', f'cache/{self.bucket}/{key}.tar.gz'])


class GHCRCache(CacheBackend):
    def __init__(self, config: CacheConfig):
        self.registry = config.ghcr_registry
        self.namespace = config.ghcr_namespace
        self.token = config.ghcr_token
    
    def _get_image_name(self, key: str) -> str:
        return f"{self.registry}/{self.namespace}/cache:{key}"
    
    def _docker_command(self, command: list) -> bool:
        """Run Docker command with authentication"""
        try:
            if self.token:
                login_result = subprocess.run([
                    'docker', 'login', self.registry, 
                    '-u', 'token', '-p', self.token
                ], capture_output=True, text=True)
                
                if login_result.returncode != 0:
                    return False
            
            result = subprocess.run(
                ['docker'] + command,
                capture_output=True,
                text=True
            )
            return result.returncode == 0
        except Exception:
            return False
    
    def exists(self, key: str) -> bool:
        image_name = self._get_image_name(key)
        return self._docker_command(['manifest', 'inspect', image_name])
    
    def get(self, key: str, local_path: str) -> bool:
        image_name = self._get_image_name(key)
        
        # Pull image
        if not self._docker_command(['pull', image_name]):
            return False
        
        # Export to tar
        try:
            result = subprocess.run([
                'docker', 'save', '-o', local_path, image_name
            ], capture_output=True, text=True)
            return result.returncode == 0
        except Exception:
            return False
    
    def put(self, key: str, local_path: str) -> bool:
        image_name = self._get_image_name(key)
        
        try:
            # Load from tar
            result = subprocess.run([
                'docker', 'load', '-i', local_path
            ], capture_output=True, text=True)
            
            if result.returncode != 0:
                return False
            
            # Tag and push
            return self._docker_command(['push', image_name])
        except Exception:
            return False
    
    def delete(self, key: str) -> bool:
        # GHCR doesn't support direct deletion via Docker CLI
        # Would need to use GitHub API for deletion
        return False


class CacheManager:
    def __init__(self, config: CacheConfig):
        self.config = config
        self.local_cache = LocalCache(config.local_path)
        self.minio_cache = MinioCache(config) if config.minio_endpoint else None
        self.ghcr_cache = GHCRCache(config) if config.ghcr_namespace else None
    
    def _get_backend(self, cache_level: CacheLevel) -> Optional[CacheBackend]:
        """Get cache backend for specified level"""
        if cache_level == CacheLevel.LOCAL:
            return self.local_cache
        elif cache_level == CacheLevel.MINIO:
            return self.minio_cache
        elif cache_level == CacheLevel.GHCR:
            return self.ghcr_cache
        return None
    
    def exists(self, key: str, cache_level: CacheLevel) -> bool:
        """Check if cache entry exists at specified level"""
        backend = self._get_backend(cache_level)
        if not backend:
            return False
        return backend.exists(key)
    
    def get(self, key: str, cache_level: CacheLevel) -> Optional[str]:
        """Get cache entry from specified level, returns local path if successful"""
        backend = self._get_backend(cache_level)
        if not backend:
            return None
        
        with tempfile.NamedTemporaryFile(suffix='.tar.gz', delete=False) as temp_file:
            if backend.get(key, temp_file.name):
                return temp_file.name
        
        return None
    
    def put(self, key: str, local_path: str, cache_level: CacheLevel) -> bool:
        """Put cache entry to specified level"""
        backend = self._get_backend(cache_level)
        if not backend:
            return False
        return backend.put(key, local_path)
    
    def get_best_available(self, key: str) -> tuple[Optional[str], Optional[CacheLevel]]:
        """Get cache entry from best available level (GHCR > Minio > Local)"""
        for level in [CacheLevel.GHCR, CacheLevel.MINIO, CacheLevel.LOCAL]:
            if self.exists(key, level):
                cached_path = self.get(key, level)
                if cached_path:
                    return cached_path, level
        return None, None
    
    def promote_cache(self, key: str, from_level: CacheLevel, to_level: CacheLevel) -> bool:
        """Promote cache entry from lower to higher level"""
        if from_level == to_level:
            return True
        
        cached_path = self.get(key, from_level)
        if not cached_path:
            return False
        
        try:
            return self.put(key, cached_path, to_level)
        finally:
            if os.path.exists(cached_path):
                os.unlink(cached_path)
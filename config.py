from dataclasses import dataclass
from typing import Dict, List, Optional, Any
from enum import Enum


class CacheLevel(Enum):
    LOCAL = "local"
    MINIO = "minio" 
    GHCR = "ghcr"


@dataclass
class UserDeclaration:
    user: str = "app"
    sudo: bool = False
    apt_packages: List[str] = None
    yum_packages: List[str] = None
    env_scripts: List[str] = None
    stages: List['Stage'] = None
    image_name: str = "my-app"
    container_name: str = "my-app-container"
    image_tag: str = "latest"
    inherit_env: bool = True
    inherit_proxy: bool = True
    inherit_locale: bool = False
    inherit_timezone: bool = True
    inherit_custom_env: List[str] = None
    exclude_env: List[str] = None
    
    def __post_init__(self):
        if self.apt_packages is None:
            self.apt_packages = []
        if self.yum_packages is None:
            self.yum_packages = []
        if self.env_scripts is None:
            self.env_scripts = []
        if self.stages is None:
            self.stages = []
        if self.inherit_custom_env is None:
            self.inherit_custom_env = []
        if self.exclude_env is None:
            self.exclude_env = []


@dataclass
class Stage:
    name: str
    dependencies: List[str] = None
    commands: List[str] = None
    
    def __post_init__(self):
        if self.dependencies is None:
            self.dependencies = []
        if self.commands is None:
            self.commands = []


@dataclass
class BuildStep:
    stage_name: str
    command: str
    hash: str
    cached: bool = False
    cache_level: Optional[CacheLevel] = None


@dataclass
class CacheConfig:
    local_path: str = "/tmp/docker-cache"
    minio_endpoint: str = ""
    minio_bucket: str = "docker-cache"
    minio_access_key: str = ""
    minio_secret_key: str = ""
    ghcr_registry: str = "ghcr.io"
    ghcr_namespace: str = ""
    ghcr_token: str = ""
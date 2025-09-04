from dataclasses import dataclass
from typing import Dict, List, Optional, Any
from enum import Enum
import hashlib


class CacheLevel(Enum):
    LOCAL = "local"
    MINIO = "minio" 
    GHCR = "ghcr"


class LayerType(Enum):
    BASE = "base"
    APT = "apt"
    YUM = "yum"
    PIP = "pip"
    SCRIPT = "script"
    CONFIG = "config"
    BATCH = "batch"


@dataclass
class Layer:
    """Represents a single Docker build layer"""
    name: str
    type: LayerType
    content: str  # Package name or script content
    parent: Optional[str] = None
    hash: Optional[str] = None
    image_tag: Optional[str] = None
    dependencies: List[str] = None
    
    def __post_init__(self):
        if self.dependencies is None:
            self.dependencies = []
        if self.hash is None:
            self.hash = self.calculate_hash()
    
    def calculate_hash(self) -> str:
        """Calculate hash based on layer content"""
        content = f"{self.type.value}:{self.name}:{self.content}"
        return hashlib.sha256(content.encode()).hexdigest()[:8]
    
    def get_image_tag(self, image_name: str) -> str:
        """Generate image tag for this layer"""
        return f"{image_name}:layer-{self.type.value}-{self.name}-{self.hash}"


@dataclass
class LightSetupConfig:
    """Light setup configuration for fast operations like file modifications"""
    name: str
    dependencies: List[str] = None
    commands: List[str] = None
    
    def __post_init__(self):
        if self.dependencies is None:
            self.dependencies = []
        if self.commands is None:
            self.commands = []


@dataclass
class ScriptInstall:
    """Script-based installation configuration"""
    name: str
    dependencies: List[str] = None
    commands: List[str] = None
    remove_commands: List[str] = None  # Commands to safely remove this script's effects
    
    def __post_init__(self):
        if self.dependencies is None:
            self.dependencies = []
        if self.commands is None:
            self.commands = []
        if self.remove_commands is None:
            self.remove_commands = []


@dataclass
class HeavySetup:
    """Heavy setup configuration for package installations and script-based installs"""
    apt_packages: List[str] = None
    yum_packages: List[str] = None
    script_installs: List[ScriptInstall] = None
    
    def __post_init__(self):
        if self.apt_packages is None:
            self.apt_packages = []
        if self.yum_packages is None:
            self.yum_packages = []
        if self.script_installs is None:
            self.script_installs = []


@dataclass
class UserDeclaration:
    user: str = "app"
    sudo: bool = False
    
    # Base image
    base_image: str = "ubuntu:22.04"
    
    # Legacy fields for backward compatibility
    apt_packages: List[str] = None
    yum_packages: List[str] = None
    env_scripts: List[str] = None
    stages: List['Stage'] = None
    
    # New layer-based structure
    layers: Dict[str, Any] = None  # For layered mode
    light_setup: Dict[str, List[LightSetupConfig]] = None
    heavy_setup: HeavySetup = None
    
    # Optimization settings
    optimization: Dict[str, Any] = None
    
    # Metadata
    image_name: str = "my-app"
    container_name: str = "my-app-container"
    image_tag: str = "latest"
    
    # Environment configuration
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

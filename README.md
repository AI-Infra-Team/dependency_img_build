# Dynamic Docker Build System

A Python-based system for building Docker images with dynamic Dockerfiles and multi-level caching (GHCR, local-area-net MinIO).
通过统一描述依赖项和阶段描述来支持动态构造dockerfile进行安装，并遵循依赖顺序
同时会记录下每一次的安装顺序，有靠前变更，会将变更追加到后面避免全量重构
以上思路可以使高频变更项统一被排到后面，避免镜像的反复重构
如果变更的不是最后一个stage。整个stage都会被迁移到后面

## Features

- **Dynamic Dockerfile Generation**: Users declare what they need, system generates optimized Dockerfiles
- **Multi-level Caching**: Local, MinIO, and GitHub Container Registry (GHCR) cache support  
- **Smart Rebuild Detection**: Only rebuilds changed steps and subsequent ones
- **Stage Dependencies**: Define installation stages with dependency ordering
- **Cache Efficiency Tracking**: Monitor cache hit rates and build optimization

## Quick Start

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Create example configuration**:
   ```bash
   python cli.py init
   ```

3. **Build your image**:
   ```bash
   python cli.py build -c build-config.json -t myapp:latest
   ```

## Configuration Format

Create a JSON or YAML file describing your build requirements:

```json
{
  "user": "app",
  "sudo": true,
  "apt_packages": ["curl", "git", "python3"],
  "yum_packages": [],
  "env_scripts": ["pip3 install --upgrade pip"],
  "stages": [
    {
      "name": "dependencies", 
      "dependencies": [],
      "commands": ["pip3 install requests flask"]
    },
    {
      "name": "application",
      "dependencies": ["dependencies"], 
      "commands": ["mkdir -p /app", "cp . /app"]
    }
  ]
}
```

## CLI Commands

- `build -c CONFIG -t TAG`: Build Docker image
- `status [-c CONFIG]`: Show build status and cache efficiency
- `clean [--max-age DAYS]`: Clean old cache entries
- `init [-o OUTPUT]`: Create example configuration

## Cache Configuration

Configure caching via environment variables or config file:

```bash
export CACHE_LOCAL_PATH=/tmp/docker-cache
export MINIO_ENDPOINT=minio.example.com:9000
export MINIO_BUCKET=docker-cache
export GHCR_NAMESPACE=myorg
```

## How It Works

1. **Parse Configuration**: Validates user declaration and resolves stage dependencies
2. **Generate Build Plan**: Creates build steps with content hashes for cache keys
3. **Analyze Changes**: Compares with previous builds to determine what needs rebuilding
4. **Execute Build**: Generates optimized Dockerfile and runs Docker build
5. **Update Cache**: Records successful builds and promotes cache entries

## Architecture

- `config.py`: Data structures and configuration
- `parser.py`: Configuration parsing and validation  
- `dockerfile_generator.py`: Dynamic Dockerfile generation
- `build_tracker.py`: Build history and change detection
- `cache_manager.py`: Multi-level cache backends
- `build_orchestrator.py`: Main build coordination
- `cli.py`: Command-line interface
# Dynamic Docker Build System - 分层镜像架构

## 核心目标
彻底解决 Docker 构建缓存问题，实现真正的增量构建。

## 核心问题与解决方案

### 问题分析
传统单 Dockerfile 方式的致命缺陷：
- 所有步骤在一个 Dockerfile 中，任何变更（增加/删除/注释）导致后续所有层失效
- Docker 层缓存是线性的，无法实现真正的增量构建
- 例如：注释掉一个 apt 包，会导致全部重建

### 解决方案：分层镜像架构
**核心理念：每个包/步骤 = 独立镜像**

```
ubuntu:22.04
    ↓
myapp:layer-curl
    ↓
myapp:layer-git  
    ↓
myapp:layer-vim
    ↓
myapp:latest
```

## 实现架构

### 1. 镜像层管理

#### 镜像命名规则
```
{image_name}:layer-{type}-{name}-{hash[:8]}
```
例如：
- `myapp:layer-base-ubuntu-a1b2c3d4`
- `myapp:layer-apt-curl-e5f6g7h8`
- `myapp:layer-script-rust-m3n4o5p6`

#### 层类型
- **base**: 基础镜像层
- **apt**: APT 包安装层
- **yum**: YUM 包安装层  
- **script**: 脚本执行层
- **config**: 配置文件层

### 2. 增量构建机制

#### 添加包场景
```bash
# 原始配置
apt_packages: [curl, git, vim]

# 添加 wget
apt_packages: [curl, git, wget, vim]

# 构建策略
1. 复用 myapp:layer-apt-git
2. 创建新层 myapp:layer-apt-wget (基于 git 层)
3. 重建 myapp:layer-apt-vim (基于 wget 层)
```

#### 删除包场景
```bash
# 原始配置  
apt_packages: [curl, git, wget, vim]

# 删除 wget
apt_packages: [curl, git, vim]

# 构建策略
1. 复用 myapp:layer-apt-git
2. 跳过 wget 层
3. 重建 myapp:layer-apt-vim (直接基于 git 层)
```

### 3. Dockerfile 生成策略

每个层生成独立的 Dockerfile：

```dockerfile
# Dockerfile.layer-apt-curl
FROM myapp:layer-base-ubuntu-a1b2c3d4
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# Dockerfile.layer-apt-git  
FROM myapp:layer-apt-curl-e5f6g7h8
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*
```

### 4. 并行构建

独立的包可以并行构建：
```
      base
     /    \
  curl    wget
     \    /
      git
```

## 缓存管理

### layers_cache.json 数据结构
```json
{
  "layers": {
    "apt-curl": {
      "hash": "e5f6g7h8",
      "parent": "base-ubuntu-a1b2c3d4",
      "image": "myapp:layer-apt-curl-e5f6g7h8",
      "created": "2024-01-01T10:00:00Z",
      "size": "50MB",
      "status": "active"
    }
  },
  "dependency_graph": {
    "base-ubuntu": [],
    "apt-curl": ["base-ubuntu"],
    "apt-git": ["apt-curl"]
  },
  "layer_chains": {
    "myapp:latest": [
      "base-ubuntu-a1b2c3d4",
      "apt-curl-e5f6g7h8",
      "apt-git-i9j0k1l2"
    ]
  }
}
```

## 优化策略

### 层合并
当层数过多时，批量合并：
```
myapp:layer-apt-batch1 (包含 10 个包)
myapp:layer-apt-batch2 (包含下一批 10 个包)
```

### 清理策略
- 定期清理未使用的中间层
- 保留活跃的层链
- 基于 LRU 策略管理缓存

## 配置格式

```yaml
# 镜像配置
image_name: "myapp"
image_tag: "latest"

# 层定义
layers:
  apt:
    - curl
    - git
    - vim
    
  apt_batch:
    # 批量安装作为一层
    - [net-tools, iputils-ping, dnsutils]
    
  scripts:
    - name: install_rust
      commands:
        - curl --proto "=https" --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
        
  config:
    - name: ssh_setup
      commands:
        - mkdir -p /var/run/sshd
        - sed -i 's/Port 22/Port 2222/' /etc/ssh/sshd_config

# 优化配置
optimization:
  max_layers: 50      # 最大层数限制
  batch_size: 10      # 批量合并大小
  parallel: true      # 启用并行构建
  cleanup_age: "7d"   # 清理超过 7 天的未使用层
```

## CLI 接口

### 构建命令
```bash
# 构建镜像
build -c config.yaml

# 查看层结构
layers --tree

# 分析层
layers --analyze

# 清理未使用层
clean-layers --older-than 7d

# 导出层链
export-chain --output chain.json
```

## 核心组件

### LayerManager (layer_manager.py)
- 管理层的依赖关系
- 计算层的哈希值
- 决定构建顺序
- 处理并行构建规划

### LayerBuilder (layer_builder.py)
- 为每个层生成 Dockerfile
- 执行 docker build
- 管理层镜像标签
- 处理构建失败和重试

### LayerCache (layer_cache.py)
- 维护 layers_cache.json
- 查询层是否存在
- 管理层的生命周期
- 实现 LRU 清理策略

### LayeredBuildOrchestrator (build_orchestrator.py)
- 协调整体构建流程
- 管理层构建顺序
- 处理并行构建
- 最终镜像标记

## 核心优势

1. **真正的增量构建** - 删除或注释包不会影响其他包的缓存
2. **灵活的依赖管理** - 可以随意调整包的顺序和依赖
3. **并行构建能力** - 独立的包可以并行构建
4. **细粒度缓存** - 每个包都是独立的缓存单元
5. **易于回滚** - 每个层都是独立的镜像

## 设计原则

1. **原子性** - 每个层是最小的构建单元
2. **幂等性** - 相同的输入产生相同的层
3. **可追溯** - 每个层都有明确的来源和历史
4. **高效性** - 最大化缓存复用，最小化重建
5. **可视化** - 清晰展示层的依赖关系
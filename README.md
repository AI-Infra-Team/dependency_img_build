# Mooncake Test Image: Build & Run (Simple)

将镜像构建与运行统一为一个脚本，直接使用 `scripts/start_test_mooncake_server.py` 完成镜像构建并启动容器（无需再使用本目录下的动态构建工具）。

## 快速开始

- 先决条件：
  - 已安装 Docker
  - Python 3.8+ 与 pip（脚本会自动安装 `pyscript-util`）

- 一条命令构建并启动：
  ```bash
  python3 scripts/start_test_mooncake_server.py
  ```

脚本将自动：
- 在项目根目录下定位并调用 `scripts/build_dev_container_img.py` 构建镜像 `unify-kvcache-dev:latest`
- 以容器名 `mooncake_test_server` 启动容器，并挂载项目到容器 `/app`
- 暴露端口：`8085:8083`、`8086:8080`、`50056:50051`
- 设置环境变量 `HOST_IP`（自动探测）

## 常用操作

- 查看日志：
  ```bash
  docker logs -f mooncake_test_server
  ```

- 停止容器：
  ```bash
  docker stop mooncake_test_server
  ```

- 仅构建镜像（不启动容器）：
  ```bash
  python3 scripts/build_dev_container_img.py
  ```

- 调整端口/行为：
  - 端口映射在 `scripts/start_test_mooncake_server.py` 中定义，可按需修改
  - 容器内入口脚本为：`/app/scripts/start_test_mooncake_server/entrypoint.py`

## 说明

- 旧的“动态 Docker 构建系统”（本目录）已不再建议使用；如仅需本项目测试环境与依赖，推荐直接使用上述脚本
- 如需高度自定义的分阶段/依赖驱动构建流程，可参考历史版本或自行扩展，但默认路径已切换为 `start_test_mooncake_server.py`

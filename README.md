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

## 依赖校验和（Checksum）与快速跳过构建

本目录提供的 dependency_img_build CLI 现已内置“依赖校验和”能力：

- 构建前：读取传入的 YAML/JSON 配置，剔除与依赖无关的键（如 apt_sources、各类 proxy），对其余内容进行 JSON 规范化后计算 sha256。
- 快速跳过：若同名 `img_dependency_{image_name}_{image_tag}.checksum` 已存在且内容与当前一致，且未指定 `--force-rebuild`，则直接跳过构建并返回成功。
- 构建后：将本次的依赖校验和写入 `img_dependency_{image_name}_{image_tag}.checksum`，供后续比较使用。

上层脚本（如 `scripts/pack_unified_pylib.py`）可读取该 checksum 判断依赖是否变化，以决定是否复用已存在的构建容器并避免不必要的重启。

忽略键包括但不限于：
- `apt_sources`
- `inherit_proxy`
- `http_proxy` / `https_proxy` / `HTTP_PROXY` / `HTTPS_PROXY`

如需扩展忽略列表或输出位置，可在 `scripts/dependency_img_build/cli.py` 中微调逻辑。

## ScriptInstall 用法（commands 与 file 二选一）

在 `heavy_setup.script_installs` 中，脚本安装支持两种写法，且互斥：

- commands: 提供一组 shell 命令（按顺序以 `RUN set -e; ... && ...` 执行）
- file: 提供一个相对 YAML 文件路径的脚本（构建时会 COPY 到镜像并执行；.py 以 python3 执行，其他以 /bin/bash 执行）

若同时提供 `commands` 和 `file`，解析阶段会抛出错误。

示例：
```yaml
heavy_setup:
  script_installs:
    - name: install_rust_with_python
      file: scripts/build_pack_unified_pylib_img/install_rust_with_python.sh
    - name: setup_other
      commands:
        - echo "hello"
        - echo "world"
```

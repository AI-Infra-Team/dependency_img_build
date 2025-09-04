FROM ubuntu:22.04

# Auto-generated Dockerfile by dependency_img_build

# Heavy Setup: APT Package Installation
# CACHE: This step should be cached
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && apt-get update && apt-get install -y openssh-server

# CACHE: This step should be cached
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && apt-get install -y sudo

# CACHE: This step should be cached
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && apt-get install -y curl

# CACHE: This step should be cached
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && apt-get install -y wget

# CACHE: This step should be cached
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && apt-get install -y git

# REBUILD: This step needs rebuilding
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && apt-get install -y vim

# REBUILD: This step needs rebuilding
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && apt-get install -y nano

# REBUILD: This step needs rebuilding
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && apt-get install -y htop

# REBUILD: This step needs rebuilding
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && apt-get install -y build-essential

# REBUILD: This step needs rebuilding
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && apt-get install -y cmake

# REBUILD: This step needs rebuilding
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && apt-get install -y pkg-config

# REBUILD: This step needs rebuilding
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && apt-get install -y libssl-dev

# REBUILD: This step needs rebuilding
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && apt-get install -y ca-certificates

# REBUILD: This step needs rebuilding
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && apt-get install -y gnupg

# REBUILD: This step needs rebuilding
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && apt-get install -y net-tools

# REBUILD: This step needs rebuilding
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && apt-get install -y g++

RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && apt-get install -y gdb

RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && apt-get install -y valgrind

RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && apt-get install -y clang

RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && apt-get install -y libboost-all-dev

RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && apt-get install -y docker.io

RUN rm -rf /var/lib/apt/lists/*

# Heavy Setup: Script Installations
# Script Install: create_user
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && useradd -m -s /bin/bash pa || true
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && echo 'pa:74123' | chpasswd
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && usermod -aG sudo pa || true
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && echo 'pa ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

# Script Install: install_rust
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && curl --proto "=https" --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && echo 'source ~/.cargo/env' >> ~/.bashrc

# Script Install: setup_docker_access
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && usermod -aG docker pa || true
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && systemctl enable docker || true

# Light Setup: Configuration Changes
# Light Setup Category: config_files
# Config: setup_ssh_config
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && mkdir -p /var/run/sshd
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && echo 'root:root123' | chpasswd
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin yes/' /etc/ssh/sshd_config
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && sed -i 's/#PasswordAuthentication yes/PasswordAuthentication yes/' /etc/ssh/sshd_config
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && sed -i 's/Port 22/Port 2222/' /etc/ssh/sshd_config

# Config: setup_environment
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && echo 'export PATH="$HOME/.cargo/bin:$PATH"' >> /home/pa/.bashrc
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && echo 'cd /opt' >> /home/pa/.bashrc

# User setup
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && useradd -m -s /bin/bash pa || true
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && apt-get update && apt-get install -y sudo
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && usermod -aG sudo pa || true
RUN echo "DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy" && echo 'pa ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers
USER pa
WORKDIR /home/pa

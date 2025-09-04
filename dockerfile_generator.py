import hashlib
import os
from typing import List
from config import UserDeclaration, Stage, BuildStep
from env_manager import EnvironmentManager, EnvVarConfig


class DockerfileGenerator:
    def __init__(self, base_image: str = "ubuntu:22.04"):
        self.base_image = base_image
    
    def generate(self, declaration: UserDeclaration, stage_order: List[str], rebuild_from_step: int = 0) -> str:
        """Generate Dockerfile content based on user declaration"""
        # Use base_image from declaration if available
        base_image = getattr(declaration, 'base_image', self.base_image)
        
        dockerfile_lines = [
            f"FROM {base_image}",
            "",
            "# Auto-generated Dockerfile by dependency_img_build",
            "",
            "# Copy utility scripts",
            "COPY scripts/apt_install.sh /usr/local/bin/apt_install.sh",
            "COPY scripts/apt_remove.sh /usr/local/bin/apt_remove.sh", 
            "RUN chmod +x /usr/local/bin/apt_install.sh /usr/local/bin/apt_remove.sh",
            ""
        ]
        
        # New structure: Heavy Setup first, Light Setup second
        if declaration.heavy_setup:
            # Heavy Setup: APT packages
            dockerfile_lines.extend(self._generate_heavy_apt_packages(declaration))
            # Heavy Setup: Script installations  
            dockerfile_lines.extend(self._generate_heavy_script_installs(declaration))
        
        # Backward compatibility: Legacy apt packages and stages
        if declaration.apt_packages:
            dockerfile_lines.extend(self._generate_base_setup(declaration))
            
        if declaration.stages:
            dockerfile_lines.extend(self._generate_stage_instructions(declaration, stage_order, rebuild_from_step))
            
        # Light Setup: Configuration changes (after heavy setup)
        if declaration.light_setup:
            dockerfile_lines.extend(self._generate_light_setup(declaration))
        
        # Legacy: Environment scripts and user setup
        if declaration.env_scripts:
            dockerfile_lines.extend(self._generate_env_scripts(declaration))
            
        dockerfile_lines.extend(self._generate_user_setup(declaration))
        
        return '\n'.join(dockerfile_lines)
    
    def _generate_inherited_env_vars(self, declaration: UserDeclaration) -> List[str]:
        """Generate inherited environment variables section"""
        lines = []
        
        # Check if environment variable inheritance is enabled
        if not getattr(declaration, 'inherit_env', True):
            return lines
        
        # Create environment manager configuration
        env_config = EnvVarConfig(
            inherit_proxy=getattr(declaration, 'inherit_proxy', True),
            inherit_locale=getattr(declaration, 'inherit_locale', False),
            inherit_timezone=getattr(declaration, 'inherit_timezone', True),
            inherit_custom=getattr(declaration, 'inherit_custom_env', []),
            exclude_vars=getattr(declaration, 'exclude_env', [])
        )
        
        env_manager = EnvironmentManager(env_config)
        inherited_vars = env_manager.extract_system_env_vars()
        
        if inherited_vars:
            env_lines = env_manager.generate_env_dockerfile_lines(inherited_vars)
            lines.extend(env_lines)
        
        return lines
    
    def _generate_base_setup(self, declaration: UserDeclaration) -> List[str]:
        """Generate base system setup commands using utility scripts"""
        lines = []
        
        if declaration.apt_packages or declaration.yum_packages:
            lines.append("# Package installation")
            
            # APT packages using apt_install.sh
            if declaration.apt_packages:
                packages_str = ' '.join(declaration.apt_packages)
                lines.extend([
                    f"RUN echo \"DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy\" && \\",
                    f"    /usr/local/bin/apt_install.sh {packages_str}",
                    ""
                ])
            
            # YUM packages (keep original logic for now)
            for i, pkg in enumerate(declaration.yum_packages):
                if i == 0:
                    # First package: update package list
                    lines.extend([
                        f"RUN echo \"DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy\" && yum update -y && yum install -y {pkg}",
                        ""
                    ])
                else:
                    # All other packages: just install
                    lines.extend([
                        f"RUN echo \"DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy\" && yum install -y {pkg}",
                        ""
                    ])
            
            # Clean up yum cache after all yum packages are installed
            if declaration.yum_packages:
                lines.extend([
                    "RUN yum clean all",
                    ""
                ])
        
        return lines
    
    def _generate_stage_instructions(self, declaration: UserDeclaration, stage_order: List[str], rebuild_from_step: int = 0) -> List[str]:
        """Generate stage-specific instructions in dependency order"""
        lines = []
        stage_dict = {stage.name: stage for stage in declaration.stages}
        
        # Calculate total steps before stages (apt packages + yum packages + env scripts)  
        pre_stage_steps = 0
        if declaration.apt_packages:
            pre_stage_steps += 1
        if declaration.yum_packages:
            pre_stage_steps += 1
        pre_stage_steps += len(declaration.env_scripts)
        
        step_count = pre_stage_steps
        env_vars_inserted = rebuild_from_step == 0
        
        for stage_name in stage_order:
            if stage_name in stage_dict:
                stage = stage_dict[stage_name]
                
                lines.append(f"# Stage: {stage.name}")
                
                if stage.dependencies:
                    lines.append(f"# Dependencies: {', '.join(stage.dependencies)}")
                
                for command in stage.commands:
                    # Check if we need to insert environment variables before this command
                    if not env_vars_inserted and rebuild_from_step > 0 and step_count == rebuild_from_step:
                        lines.append("# === Dynamic Environment Variables ===")
                        lines.extend(self._generate_inherited_env_vars(declaration))
                        lines.append("")
                        env_vars_inserted = True
                    
                    # Add debug echo for proxy variables to every RUN command
                    lines.append(f"RUN echo \"DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy\" && {command}")
                    step_count += 1
                lines.append("")
        
        return lines
    
    def _generate_user_setup(self, declaration: UserDeclaration) -> List[str]:
        """Generate user and permission setup"""
        lines = []
        
        if declaration.user != "root":
            lines.extend([
                f"# User setup",
                f"RUN echo \"DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy\" && useradd -m -s /bin/bash {declaration.user} || true",
            ])
            
            if declaration.sudo:
                lines.extend([
                    "RUN echo \"DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy\" && apt-get update && apt-get install -y sudo",
                    f"RUN echo \"DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy\" && usermod -aG sudo {declaration.user} || true",
                    f"RUN echo \"DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy\" && echo '{declaration.user} ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers",
                ])
            
            lines.extend([
                f"USER {declaration.user}",
                f"WORKDIR /home/{declaration.user}",
                ""
            ])
        
        return lines
    
    def _generate_env_scripts(self, declaration: UserDeclaration) -> List[str]:
        """Generate environment setup scripts after user creation"""
        lines = []
        
        if declaration.env_scripts:
            lines.append("# Environment setup scripts")
            for script in declaration.env_scripts:
                lines.append(f"RUN echo \"DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy\" && {script}")
            lines.append("")
        
        return lines
    
    def generate_build_steps(self, declaration: UserDeclaration, stage_order: List[str]) -> List[BuildStep]:
        """Generate build steps with hashes for caching"""
        steps = []
        
        # New structure: Heavy Setup first
        if declaration.heavy_setup:
            # Heavy Setup: APT packages - each package is an independent step
            for i, pkg in enumerate(declaration.heavy_setup.apt_packages):
                if i == 0:
                    # First package: update package list
                    cmd = f"apt-get update && apt-get install -y {pkg}"
                else:
                    # All other packages: just install
                    cmd = f"apt-get install -y {pkg}"
                
                steps.append(BuildStep(
                    stage_name=f"apt_{pkg.replace('-', '_')}",
                    command=cmd,
                    hash=self._hash_command(cmd)
                ))

            # Clean up package lists after all apt packages
            if declaration.heavy_setup.apt_packages:
                steps.append(BuildStep(
                    stage_name="apt_cleanup",
                    command="rm -rf /var/lib/apt/lists/*",
                    hash=self._hash_command("apt_cleanup")
                ))

            # Heavy Setup: Script installations
            for script_install in declaration.heavy_setup.script_installs:
                for i, command in enumerate(script_install.commands):
                    steps.append(BuildStep(
                        stage_name=f"{script_install.name}_{i}" if len(script_install.commands) > 1 else script_install.name,
                        command=command,
                        hash=self._hash_command(f"{script_install.name}:{command}")
                    ))
        
        # Light Setup: Configuration changes
        if declaration.light_setup:
            for category, configs in declaration.light_setup.items():
                for config in configs:
                    for i, command in enumerate(config.commands):
                        steps.append(BuildStep(
                            stage_name=f"light_{config.name}_{i}" if len(config.commands) > 1 else f"light_{config.name}",
                            command=command,
                            hash=self._hash_command(f"light_{config.name}:{command}")
                        ))

        # Legacy support for backward compatibility
        if declaration.apt_packages:
            # Base setup steps - each package is an independent step
            for i, pkg in enumerate(declaration.apt_packages):
                if i == 0:
                    # First package: update package list
                    cmd = f"apt-get update && apt-get install -y {pkg}"
                else:
                    # All other packages: just install
                    cmd = f"apt-get install -y {pkg}"
                
                steps.append(BuildStep(
                    stage_name=f"apt_{pkg.replace('-', '_')}",
                    command=cmd,
                    hash=self._hash_command(cmd)
                ))

            # Clean up package lists after all apt packages
            steps.append(BuildStep(
                stage_name="apt_cleanup",
                command="rm -rf /var/lib/apt/lists/*",
                hash=self._hash_command("apt_cleanup")
            ))

        for i, pkg in enumerate(declaration.yum_packages):
            if i == 0:
                # First package: update package list
                cmd = f"yum update -y && yum install -y {pkg}"
            else:
                # All other packages: just install
                cmd = f"yum install -y {pkg}"
                
            steps.append(BuildStep(
                stage_name=f"yum_{pkg.replace('-', '_')}", 
                command=cmd,
                hash=self._hash_command(cmd)
            ))
        
        # Clean up yum cache after all yum packages
        if declaration.yum_packages:
            steps.append(BuildStep(
                stage_name="yum_cleanup",
                command="yum clean all",
                hash=self._hash_command("yum_cleanup")
            ))
        
        # Environment scripts
        for i, script in enumerate(declaration.env_scripts):
            steps.append(BuildStep(
                stage_name=f"env_script_{i}",
                command=script,
                hash=self._hash_command(script)
            ))
        
        # Stage-specific commands (legacy)
        stage_dict = {stage.name: stage for stage in declaration.stages}
        for stage_name in stage_order:
            if stage_name in stage_dict:
                stage = stage_dict[stage_name]
                for command in stage.commands:
                    steps.append(BuildStep(
                        stage_name=stage.name,
                        command=command,
                        hash=self._hash_command(f"{stage.name}:{command}")
                    ))
        
        return steps
    
    def _hash_command(self, command: str) -> str:
        """Generate hash for a command for caching purposes"""
        return hashlib.sha256(command.encode('utf-8')).hexdigest()[:12]
    
    def _generate_heavy_apt_packages(self, declaration: UserDeclaration) -> List[str]:
        """Generate heavy setup APT package installations using apt_install.sh"""
        lines = []
        if not declaration.heavy_setup or not declaration.heavy_setup.apt_packages:
            return lines
            
        lines.append("# Heavy Setup: APT Package Installation")
        
        # Group packages for efficient installation
        packages_str = ' '.join(declaration.heavy_setup.apt_packages)
        lines.extend([
            f"RUN echo \"DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy\" && \\",
            f"    /usr/local/bin/apt_install.sh {packages_str}",
            ""
        ])
        
        return lines
    
    def _generate_heavy_script_installs(self, declaration: UserDeclaration) -> List[str]:
        """Generate heavy setup script installations"""
        lines = []
        if not declaration.heavy_setup or not declaration.heavy_setup.script_installs:
            return lines
            
        lines.append("# Heavy Setup: Script Installations")
        
        for script_install in declaration.heavy_setup.script_installs:
            lines.append(f"# Script Install: {script_install.name}")
            
            for command in script_install.commands:
                lines.append(f"RUN echo \"DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy\" && {command}")
            lines.append("")
        
        return lines
    
    def _generate_script_remove_if_available(self, script_names: List[str], declaration: UserDeclaration) -> List[str]:
        """Generate script removal commands if remove_commands are available"""
        lines = []
        if not script_names:
            return lines
            
        lines.append("# Script Removal (if remove_commands available)")
        
        # Find ScriptInstall objects by name
        script_installs = []
        if declaration.heavy_setup and declaration.heavy_setup.script_installs:
            script_installs.extend(declaration.heavy_setup.script_installs)
            
        script_map = {script.name: script for script in script_installs}
        
        for script_name in script_names:
            if script_name in script_map and script_map[script_name].remove_commands:
                script = script_map[script_name]
                lines.append(f"# Removing script: {script.name}")
                
                for remove_cmd in script.remove_commands:
                    lines.append(f"RUN echo \"DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy\" && {remove_cmd}")
                lines.append("")
            else:
                lines.append(f"# Script {script_name} has no remove_commands, skipping removal for safety")
                lines.append("")
        
        return lines
    
    def _generate_apt_remove_if_safe(self, packages_to_remove: List[str]) -> List[str]:
        """Generate safe apt remove commands using apt_remove.sh"""
        lines = []
        if not packages_to_remove:
            return lines
            
        lines.append("# Safe APT Package Removal")
        
        packages_str = ' '.join(packages_to_remove)
        lines.extend([
            f"RUN echo \"DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy\" && \\",
            f"    /usr/local/bin/apt_remove.sh {packages_str}",
            ""
        ])
        
        return lines
    
    def _generate_light_setup(self, declaration: UserDeclaration) -> List[str]:
        """Generate light setup configuration changes"""
        lines = []
        if not declaration.light_setup:
            return lines
            
        lines.append("# Light Setup: Configuration Changes")
        
        for category, configs in declaration.light_setup.items():
            lines.append(f"# Light Setup Category: {category}")
            
            for config in configs:
                lines.append(f"# Config: {config.name}")
                
                for command in config.commands:
                    lines.append(f"RUN echo \"DEBUG: http_proxy=$http_proxy https_proxy=$https_proxy\" && {command}")
                lines.append("")
        
        return lines
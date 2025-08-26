import hashlib
import os
from typing import List
from config import UserDeclaration, Stage, BuildStep
from env_manager import EnvironmentManager, EnvVarConfig


class DockerfileGenerator:
    def __init__(self, base_image: str = "ubuntu:22.04"):
        self.base_image = base_image
    
    def generate(self, declaration: UserDeclaration, stage_order: List[str]) -> str:
        """Generate Dockerfile content based on user declaration"""
        # Use base_image from declaration if available
        base_image = getattr(declaration, 'base_image', self.base_image)
        
        dockerfile_lines = [
            f"FROM {base_image}",
            "",
            "# Auto-generated Dockerfile by dependency_img_build",
            ""
        ]
        
        # Add inherited environment variables early in the build
        dockerfile_lines.extend(self._generate_inherited_env_vars(declaration))
        dockerfile_lines.extend(self._generate_base_setup(declaration))
        dockerfile_lines.extend(self._generate_stage_instructions(declaration, stage_order))
        dockerfile_lines.extend(self._generate_user_setup(declaration))
        dockerfile_lines.extend(self._generate_env_scripts(declaration))
        
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
        """Generate base system setup commands"""
        lines = []
        
        if declaration.apt_packages or declaration.yum_packages:
            lines.append("# Package installation")
            
            if declaration.apt_packages:
                lines.extend([
                    "RUN apt-get update && apt-get install -y \\",
                    "    " + " \\\n    ".join(declaration.apt_packages) + " \\",
                    "    && rm -rf /var/lib/apt/lists/*",
                    ""
                ])
            
            if declaration.yum_packages:
                lines.extend([
                    "RUN yum update -y && yum install -y \\",
                    "    " + " \\\n    ".join(declaration.yum_packages) + " \\",
                    "    && yum clean all",
                    ""
                ])
        
        return lines
    
    def _generate_stage_instructions(self, declaration: UserDeclaration, stage_order: List[str]) -> List[str]:
        """Generate stage-specific instructions in dependency order"""
        lines = []
        stage_dict = {stage.name: stage for stage in declaration.stages}
        
        for stage_name in stage_order:
            if stage_name in stage_dict:
                stage = stage_dict[stage_name]
                lines.append(f"# Stage: {stage.name}")
                
                if stage.dependencies:
                    lines.append(f"# Dependencies: {', '.join(stage.dependencies)}")
                
                for command in stage.commands:
                    lines.append(f"RUN {command}")
                lines.append("")
        
        return lines
    
    def _generate_user_setup(self, declaration: UserDeclaration) -> List[str]:
        """Generate user and permission setup"""
        lines = []
        
        if declaration.user != "root":
            lines.extend([
                f"# User setup",
                f"RUN useradd -m -s /bin/bash {declaration.user}",
            ])
            
            if declaration.sudo:
                lines.extend([
                    "RUN apt-get update && apt-get install -y sudo",
                    f"RUN usermod -aG sudo {declaration.user}",
                    f"RUN echo '{declaration.user} ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers",
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
                lines.append(f"RUN {script}")
            lines.append("")
        
        return lines
    
    def generate_build_steps(self, declaration: UserDeclaration, stage_order: List[str]) -> List[BuildStep]:
        """Generate build steps with hashes for caching"""
        steps = []
        
        # Base setup steps
        if declaration.apt_packages:
            cmd = f"apt-get update && apt-get install -y {' '.join(declaration.apt_packages)}"
            steps.append(BuildStep(
                stage_name="base_apt",
                command=cmd,
                hash=self._hash_command(cmd)
            ))
        
        if declaration.yum_packages:
            cmd = f"yum update -y && yum install -y {' '.join(declaration.yum_packages)}"
            steps.append(BuildStep(
                stage_name="base_yum", 
                command=cmd,
                hash=self._hash_command(cmd)
            ))
        
        # Environment scripts
        for i, script in enumerate(declaration.env_scripts):
            steps.append(BuildStep(
                stage_name=f"env_script_{i}",
                command=script,
                hash=self._hash_command(script)
            ))
        
        # Stage-specific commands
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
import yaml
import json
from typing import Dict, Any
from config import UserDeclaration, Stage, LightSetupConfig, ScriptInstall, HeavySetup


class DeclarationParser:
    def __init__(self):
        pass
    
    def parse_yaml(self, file_path: str) -> UserDeclaration:
        """Parse YAML configuration file into UserDeclaration"""
        with open(file_path, 'r') as f:
            data = yaml.safe_load(f)
        return self._parse_dict(data)
    
    def parse_json(self, file_path: str) -> UserDeclaration:
        """Parse JSON configuration file into UserDeclaration"""
        with open(file_path, 'r') as f:
            data = json.load(f)
        return self._parse_dict(data)
    
    def _parse_dict(self, data: Dict[str, Any]) -> UserDeclaration:
        """Convert dictionary to UserDeclaration object"""
        # Parse legacy stages format (for backward compatibility)
        stages = []
        if 'stages' in data:
            for stage_data in data['stages']:
                stage = Stage(
                    name=stage_data['name'],
                    dependencies=stage_data.get('dependencies', []),
                    commands=stage_data.get('commands', [])
                )
                stages.append(stage)
        
        # Parse new light_setup format
        light_setup = None
        if 'light_setup' in data:
            light_setup = {}
            for category, configs in data['light_setup'].items():
                light_configs = []
                for config_data in configs:
                    config = LightSetupConfig(
                        name=config_data['name'],
                        dependencies=config_data.get('dependencies', []),
                        commands=config_data.get('commands', [])
                    )
                    light_configs.append(config)
                light_setup[category] = light_configs
        
        # Parse new heavy_setup format
        heavy_setup = None
        if 'heavy_setup' in data:
            heavy_data = data['heavy_setup']
            script_installs = []
            if 'script_installs' in heavy_data:
                for install_data in heavy_data['script_installs']:
                    # commands 与 file 二选一；若同时存在则报错
                    file_path = install_data.get('file')
                    commands_list = install_data.get('commands', [])
                    if file_path and commands_list:
                        raise ValueError(
                            f"script_installs[{install_data.get('name','<unnamed>')}] cannot specify both 'file' and 'commands'"
                        )
                    commands = commands_list if not file_path else []
                    install = ScriptInstall(
                        name=install_data['name'],
                        dependencies=install_data.get('dependencies', []),
                        commands=commands,
                        file=file_path,
                        copies=install_data.get('copies', [])
                    )
                    script_installs.append(install)
            
            heavy_setup = HeavySetup(
                apt_packages=heavy_data.get('apt_packages', []),
                yum_packages=heavy_data.get('yum_packages', []),
                pip_packages=heavy_data.get('pip_packages', []),
                script_installs=script_installs
            )
        
        return UserDeclaration(
            user=data.get('user', 'app'),
            sudo=data.get('sudo', False),
            # Base image
            base_image=data.get('base_image', 'ubuntu:22.04'),
            # Legacy fields for backward compatibility
            apt_packages=data.get('apt_packages', []),
            yum_packages=data.get('yum_packages', []),
            env_scripts=data.get('env_scripts', []),
            stages=stages,
            # New fields
            layers=data.get('layers', {}),
            light_setup=light_setup,
            heavy_setup=heavy_setup,
            optimization=data.get('optimization', {}),
            # Metadata
            image_name=data.get('image_name', 'my-app'),
            container_name=data.get('container_name', 'my-app-container'),
            image_tag=data.get('image_tag', 'latest'),
            # Environment configuration
            inherit_env=data.get('inherit_env', True),
            inherit_proxy=data.get('inherit_proxy', True),
            inherit_locale=data.get('inherit_locale', False),
            inherit_timezone=data.get('inherit_timezone', True),
            inherit_custom_env=data.get('inherit_custom_env', []),
            exclude_env=data.get('exclude_env', [])
            ,apt_sources=data.get('apt_sources', [])
        )
    
    def validate_declaration(self, declaration: UserDeclaration) -> bool:
        """Validate the declaration for circular dependencies and other issues"""
        stage_names = {stage.name for stage in declaration.stages}
        
        for stage in declaration.stages:
            for dep in stage.dependencies:
                if dep not in stage_names:
                    raise ValueError(f"Stage '{stage.name}' depends on unknown stage '{dep}'")
        
        if self._has_circular_dependencies(declaration.stages):
            raise ValueError("Circular dependencies detected in stages")
        
        return True
    
    def _has_circular_dependencies(self, stages: list) -> bool:
        """Check for circular dependencies using DFS"""
        graph = {stage.name: stage.dependencies for stage in stages}
        visited = set()
        rec_stack = set()
        
        def dfs(node, depth=0):
            print(f"[dfs] depth={depth} visiting={node} deps={graph.get(node, [])}")
            visited.add(node)
            rec_stack.add(node)
            
            for neighbor in graph.get(node, []):
                if neighbor not in visited:
                    if dfs(neighbor, depth + 1):
                        return True
                elif neighbor in rec_stack:
                    print(f"[dfs] depth={depth} back-edge detected: {node} -> {neighbor}")
                    return True
            
            rec_stack.remove(node)
            return False
        
        for stage in graph:
            if stage not in visited:
                if dfs(stage, 0):
                    return True
        return False
    
    def get_stage_order(self, declaration: UserDeclaration, build_tracker=None) -> list:
        """Get stages in dependency order with dynamic reordering based on change history"""
        if build_tracker is None:
            return self._get_topological_order(declaration)
        
        # Get base topological order
        base_order = self._get_topological_order(declaration)
        
        # Apply dynamic reordering based on change history
        return self._apply_dynamic_reordering(declaration, base_order, build_tracker)
    
    def _get_topological_order(self, declaration: UserDeclaration) -> list:
        """Get basic topological sort order"""
        graph = {stage.name: stage.dependencies for stage in declaration.stages}
        in_degree = {stage.name: 0 for stage in declaration.stages}
        
        for stage in declaration.stages:
            for dep in stage.dependencies:
                in_degree[stage.name] += 1
        
        queue = [stage for stage, degree in in_degree.items() if degree == 0]
        result = []
        
        while queue:
            node = queue.pop(0)
            result.append(node)
            
            for stage in declaration.stages:
                if node in stage.dependencies:
                    in_degree[stage.name] -= 1
                    if in_degree[stage.name] == 0:
                        queue.append(stage.name)
        
        return result
    
    def _apply_dynamic_reordering(self, declaration: UserDeclaration, base_order: list, build_tracker) -> list:
        """Apply dynamic reordering based on change history and frequency"""
        stage_dict = {stage.name: stage for stage in declaration.stages}
        
        # Get change frequency for each stage
        change_frequency = build_tracker.get_stage_change_frequency()
        
        # Find stages that changed in last build
        changed_stages = build_tracker.get_last_changed_stages(declaration.stages)
        
        # Separate stages into stable and frequently changing
        stable_stages = []
        frequent_stages = []
        
        for stage_name in base_order:
            if stage_name in change_frequency and change_frequency[stage_name] > 0.3:  # >30% change rate
                frequent_stages.append(stage_name)
            else:
                stable_stages.append(stage_name)
        
        # For changed stages that are not at the end, move them to the end
        reordered = []
        moved_stages = set()
        
        # First, add stable stages that haven't changed recently
        for stage_name in stable_stages:
            if stage_name not in changed_stages:
                if self._can_place_stage(stage_name, reordered, stage_dict):
                    reordered.append(stage_name)
        
        # Then add remaining stable stages
        for stage_name in stable_stages:
            if stage_name not in reordered:
                if self._can_place_stage(stage_name, reordered, stage_dict):
                    reordered.append(stage_name)
        
        # Finally, add frequent and changed stages at the end
        remaining_stages = [s for s in base_order if s not in reordered]
        
        # Sort remaining by dependency order
        remaining_ordered = self._sort_by_dependencies(remaining_stages, stage_dict)
        reordered.extend(remaining_ordered)
        
        return reordered
    
    def _can_place_stage(self, stage_name: str, current_order: list, stage_dict: dict) -> bool:
        """Check if a stage can be placed given current order"""
        stage = stage_dict[stage_name]
        for dep in stage.dependencies:
            if dep not in current_order:
                return False
        return True
    
    def _sort_by_dependencies(self, stages: list, stage_dict: dict) -> list:
        """Sort stages by their dependencies"""
        result = []
        remaining = stages.copy()
        
        while remaining:
            placed_any = False
            for stage_name in remaining[:]:
                if self._can_place_stage(stage_name, result, stage_dict):
                    result.append(stage_name)
                    remaining.remove(stage_name)
                    placed_any = True
            
            if not placed_any:
                # Circular dependency or other issue, just add remaining in order
                result.extend(remaining)
                break
        
        return result

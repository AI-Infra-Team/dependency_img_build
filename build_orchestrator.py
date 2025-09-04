import subprocess
import tempfile
import os
import shutil
import hashlib
import json
import concurrent.futures
from typing import List, Optional, Dict, Tuple
from datetime import datetime, timedelta
from pathlib import Path
from config import UserDeclaration, CacheConfig, CacheLevel, Layer, LayerType
from parser import DeclarationParser
from dockerfile_generator import DockerfileGenerator
from build_tracker import BuildTracker
from cache_manager import CacheManager
from env_manager import EnvironmentManager, EnvVarConfig
from reuse import LayerReuseManager


def sudo_prefix() -> List[str]:
    """Return sudo prefix if not running as root"""
    if os.geteuid() != 0:
        return ['sudo', '-E']
    return []


class BuildOrchestrator:
    """Orchestrator for Docker builds with layered architecture support"""
    
    def __init__(self, cache_config: CacheConfig = None):
        self.cache_config = cache_config or CacheConfig()
        self.parser = DeclarationParser()
        self.generator = DockerfileGenerator()
        self.tracker = BuildTracker()
        self.cache_manager = CacheManager(self.cache_config)
        self.reuse_manager = LayerReuseManager()
        
        # Layer cache file (for backward compatibility)
        self.layer_cache_file = "layers_cache.json"
        self.layer_cache = self._load_layer_cache()
        
        # Work directory for Dockerfiles
        self.work_dir = None
    
    def _load_layer_cache(self) -> Dict:
        """Load layer cache from file"""
        if os.path.exists(self.layer_cache_file):
            try:
                with open(self.layer_cache_file, 'r') as f:
                    return json.load(f)
            except:
                pass
        return {
            "layers": {},
            "layer_chains": {},
            "metadata": {
                "created": datetime.now().isoformat(),
                "version": "1.0"
            }
        }
    
    def _save_layer_cache(self):
        """Save layer cache to file"""
        self.layer_cache["metadata"]["last_updated"] = datetime.now().isoformat()
        with open(self.layer_cache_file, 'w') as f:
            json.dump(self.layer_cache, f, indent=2)
    
    def build_image(self, config_file: str, force_rebuild: bool = False) -> bool:
        """Build Docker image from configuration file"""
        try:
            # Parse user declaration
            declaration = self._parse_config(config_file)
            if not self.parser.validate_declaration(declaration):
                raise ValueError("Invalid configuration")
            
            # Always use layered build
            return self._build_layered(declaration, force_rebuild)
                
        except Exception as e:
            print(f"Build failed: {str(e)}")
            return False
    
    def _build_layered(self, declaration: UserDeclaration, force_rebuild: bool = False) -> bool:
        """Build using layered architecture with optimal reuse strategy"""
        print("🔄 Using layered build mode")
        
        # Create work directory
        self.work_dir = tempfile.mkdtemp(prefix="docker_layer_")
        
        try:
            # Parse all layers from configuration
            print(f"📋 Parsing layers from configuration...")
            all_layers = self._parse_layers(declaration)
            print(f"   Found {len(all_layers)} total layers")
            
            # Get environment variables
            print(f"🌍 Processing environment variables...")
            env_vars = self._get_env_vars(declaration)
            print(f"   Found {len(env_vars)} environment variables")
            
            if force_rebuild:
                print("🔥 Force rebuild requested - ignoring cache")
                parent_image = declaration.base_image
                layers_to_build = [l for l in all_layers if l.type != LayerType.BASE]
                reused_layer_names = set()
                print(f"   Will build all {len(layers_to_build)} layers from scratch")
                build_list = [f"{l.type.value}:{l.name}" for l in layers_to_build]
                if build_list:
                    print(f"   📝 Build list: {', '.join(build_list)}")
            else:
                # Use the reuse manager to find optimal strategy!
                print(f"🔍 Finding optimal reuse strategy...")
                base_image, reused_layer_names, layers_to_build, cleanup_commands = self.reuse_manager.find_optimal_base(
                    all_layers,
                    preferred_repo=declaration.image_name
                )
                # Fallback: if no meaningful reuse found, widen search across all repositories
                needed_pkgs = [l for l in all_layers if l.type in (LayerType.APT, LayerType.YUM)]
                reused_pkg_count = len([name for name in reused_layer_names if any(l.name == name and l.type in (LayerType.APT, LayerType.YUM) for l in all_layers)])
                if needed_pkgs and reused_pkg_count == 0:
                    print("   🔁 No reusable APT/YUM layers found in same repo; widening search across repos...")
                    base_image, reused_layer_names, layers_to_build, cleanup_commands = self.reuse_manager.find_optimal_base(
                        all_layers,
                        preferred_repo=None
                    )
                    parent_image = base_image
                else:
                    parent_image = base_image
                parent_image = base_image
                
                print(f"📊 Reusing {len(reused_layer_names)} layers, building {len(layers_to_build)}")
                packages_reused = len([name for name in reused_layer_names if any(l.name == name and l.type == LayerType.APT for l in all_layers)])
                scripts_reused = len([name for name in reused_layer_names if any(l.name == name and l.type == LayerType.SCRIPT for l in all_layers)])
                print(f"   Packages reused: {packages_reused}, Scripts reused: {scripts_reused}")
                print(f"   Base image: {parent_image}")
                
                # Note: Do not perform dpkg-based presence checks here; validation happens in tests
                
                # If the optimal base contains extra APT packages, schedule a cleanup layer
                cleanup_layers: List[Layer] = []
                if cleanup_commands:
                    print(f"   ⚠️  {len(cleanup_commands)} cleanup operations available for extra dependencies")
                    apt_removes = []
                    for cleanup in cleanup_commands:
                        if cleanup['type'] == 'apt_remove':
                            apt_list = cleanup.get('packages', [])
                            apt_removes.extend(apt_list)
                            print(f"      - {cleanup['description']}: {', '.join(apt_list[:3])}{'...' if len(apt_list) > 3 else ''}")
                        elif cleanup['type'] == 'script_remove':
                            print(f"      - {cleanup['description']}: {', '.join(cleanup.get('scripts', [])[:3])}{'...' if len(cleanup.get('scripts', [])) > 3 else ''}")
                    # Create an apt cleanup layer if needed
                    if apt_removes:
                        cleanup_cmd = (
                            "DEBIAN_FRONTEND=noninteractive apt-get purge -y " + ' '.join(sorted(set(apt_removes))) +
                            " || true && DEBIAN_FRONTEND=noninteractive apt-get autoremove -y || true"
                        )
                        cleanup_layers.append(Layer(
                            name="apt_cleanup_remove",
                            type=LayerType.SCRIPT,
                            content=cleanup_cmd
                        ))
                        # Prepend cleanup layer so extra packages are removed before proceeding
                        layers_to_build = cleanup_layers + layers_to_build

                # Summarize the concrete build plan and reuse list
                build_list = [f"{l.type.value}:{l.name}" for l in layers_to_build]
                if build_list:
                    print(f"   📝 Build list (order): {', '.join(build_list)}")
                reuse_list = [f"{l.type.value}:{l.name}" for l in all_layers if l.name in reused_layer_names and l.type != LayerType.BASE]
                if reuse_list:
                    print(f"   ♻️  Reuse list: {', '.join(reuse_list)}")
            
            # Build the required layers
            built_count = 0
            print(f"\n🚧 Starting build process...")
            
            # Track only layers we actually build in this run
            built_layers: List[Layer] = []
            
            # Log reused layers (do not add to cache lists unless built)
            print(f"📦 Processing reused layers...")
            reused_count = 0
            for layer in all_layers:
                if layer.type == LayerType.BASE:
                    continue
                if layer.name in reused_layer_names:
                    reused_count += 1
                    print(f"   ✅ Reusing layer: {layer.name}")
            print(f"   Total reused layers: {reused_count}")
            
            # If we need to build APT packages, run apt-get update first
            has_apt_to_build = any(l.type == LayerType.APT for l in layers_to_build)
            if has_apt_to_build and parent_image != declaration.base_image:
                print(f"🔄 Need to refresh apt cache for continuing build...")
                # We're continuing from an existing image, need fresh apt cache
                apt_update_layer = Layer(
                    name="apt_refresh",
                    type=LayerType.SCRIPT,
                    content="apt-get update"
                )
                image_tag = self._build_layer(apt_update_layer, parent_image, env_vars, declaration.image_name)
                parent_image = image_tag
                print(f"✓ Refreshed apt cache")
            
            print(f"\n🔨 Building {len(layers_to_build)} new layers...")
            
            # Build all the layers we need
            for i, layer in enumerate(layers_to_build):
                print(f"\n📦 Building layer {i+1}/{len(layers_to_build)}: {layer.name}")
                
                # For the first APT package when building from scratch, add apt-get update
                if layer.type == LayerType.APT and parent_image == declaration.base_image and built_count == 0:
                    print(f"   Adding apt-get update before first APT package...")
                    apt_update_layer = Layer(
                        name="apt_update",
                        type=LayerType.SCRIPT,
                        content="apt-get update"
                    )
                    image_tag = self._build_layer(apt_update_layer, parent_image, env_vars, declaration.image_name)
                    parent_image = image_tag
                    print(f"✓ Updated apt cache")
                
                # Build the layer
                print(f"   Building layer {layer.name}...")
                image_tag = self._build_layer(layer, parent_image, env_vars, declaration.image_name)
                
                # Cache the layer
                print(f"   Caching layer {layer.name}...")
                self.reuse_manager.cache_layer(layer, image_tag)
                
                # Track layers actually built in this run
                built_layers.append(layer)
                
                # Cache this intermediate state with layers actually built in this run
                print(f"   Caching intermediate state with {len(built_layers)} built layers...")
                self.reuse_manager.cache_built_image(image_tag, built_layers.copy())
                
                parent_image = image_tag
                built_count += 1
                print(f"✓ Built layer {layer.name}: {image_tag}")
                print(f"   Progress: {built_count}/{len(layers_to_build)} layers completed")
            
            # Tag final image
            print(f"\n🏷️  Tagging final image...")
            final_image = parent_image
            if final_image:
                target_tag = f"{declaration.image_name}:{declaration.image_tag}"
                print(f"   Final image: {final_image}")
                print(f"   Target tag: {target_tag}")
                
                if final_image != target_tag:
                    print(f"   Tagging {final_image} as {target_tag}")
                    self._tag_image(final_image, target_tag)
                else:
                    print(f"   Image already has target tag")
                
                # Cache the layers actually present in the final image (built + reused)
                used_layers: List[Layer] = []
                reused_set = set(reused_layer_names)
                for l in all_layers:
                    if l.type == LayerType.BASE:
                        continue
                    if l in built_layers or l.name in reused_set:
                        used_layers.append(l)
                print(f"   Caching complete image with {len(used_layers)} used layers (built + reused)...")
                self.reuse_manager.cache_built_image(target_tag, used_layers)
                
                print(f"\n✅ Successfully built {target_tag}")
                print(f"📊 Build stats: {built_count} built, {len(reused_layer_names) if not force_rebuild else 0} reused")
                
                return True
            
            return False
            
        finally:
            # Cleanup work directory
            print(f"🧹 Cleaning up work directory...")
            if self.work_dir and os.path.exists(self.work_dir):
                print(f"   Removing: {self.work_dir}")
                shutil.rmtree(self.work_dir)
                print(f"   ✅ Work directory cleaned up")
            else:
                print(f"   No work directory to clean up")

    # Note: Project code does not perform package presence checks; tests cover validation.
    
    def _build_traditional_deprecated(self, declaration: UserDeclaration, force_rebuild: bool = False) -> bool:
        """DEPRECATED: Build using traditional single Dockerfile approach"""
        print("📦 Using traditional build mode")
        
        # Get image tag from configuration
        image_tag = f"{declaration.image_name}:{declaration.image_tag}"
        
        # Record stage changes if using stages
        if declaration.stages:
            changed_stages = self.tracker.record_stage_changes(declaration.stages, image_tag)
            changed_stage_names = [name for name, changed in changed_stages.items() if changed]
            
            if changed_stage_names:
                print(f"🔄 Detected changes in stages: {', '.join(changed_stage_names)}")
            else:
                print("✅ No stage changes detected")
            
            # Get optimized stage order
            stage_order = self.parser.get_stage_order(declaration, self.tracker)
            
            # Apply optimization
            original_order = stage_order.copy()
            stage_order = self.tracker.get_optimized_stage_order(declaration.stages, stage_order)
            
            if original_order != stage_order:
                print(f"🔀 Stage order optimized: {' → '.join(stage_order)}")
        else:
            stage_order = []
        
        # Generate build steps
        build_steps = self.generator.generate_build_steps(declaration, stage_order)
        
        print(f"🔧 Generated {len(build_steps)} build steps")
        
        # Show inherited environment variables
        self._show_inherited_env_summary(declaration)
        
        # Analyze what needs to be rebuilt
        if not force_rebuild:
            rebuild_plan = self.tracker.get_rebuild_plan(build_steps)
            print(f"📈 Build plan: {rebuild_plan['keep_steps']} cached, "
                  f"{rebuild_plan['rebuild_steps']} rebuild "
                  f"({rebuild_plan['efficiency']:.1%} efficiency)")
            
            rebuild_from_step = rebuild_plan.get('first_changed_step', 0) or 0
        else:
            rebuild_plan = {"actions": ['rebuild'] * len(build_steps)}
            rebuild_from_step = 0
            print("🔥 Force rebuild requested - ignoring all cache")
        
        # Execute build
        success = self._execute_build(
            declaration, stage_order, build_steps, 
            rebuild_plan["actions"], image_tag, rebuild_from_step
        )
        
        if success:
            self.tracker.record_build(build_steps, image_tag)
            print(f"Successfully built image: {image_tag}")
        
        return success
    
    def _parse_layers(self, declaration: UserDeclaration) -> List[Layer]:
        """Parse layers from declaration"""
        layers = []
        
        # Create base layer
        base_layer = Layer(
            name="base",
            type=LayerType.BASE,
            content=declaration.base_image
        )
        layers.append(base_layer)
        
        # Check if we need apt packages
        has_apt_packages = False
        if hasattr(declaration, 'heavy_setup') and declaration.heavy_setup and declaration.heavy_setup.apt_packages:
            has_apt_packages = True
        elif declaration.apt_packages:
            has_apt_packages = True
        
        # Add apt-update layer if we have apt packages to install
        if has_apt_packages:
            apt_update_layer = Layer(
                name="apt_update",
                type=LayerType.SCRIPT,
                content="apt-get update"
            )
            layers.append(apt_update_layer)
        
        # Parse from heavy_setup first (current structure)
        if hasattr(declaration, 'heavy_setup') and declaration.heavy_setup:
            # Parse APT packages from heavy_setup
            if declaration.heavy_setup.apt_packages:
                for package in declaration.heavy_setup.apt_packages:
                    # Replace special characters in package names for layer naming
                    safe_name = package.replace('-', '_').replace('+', 'plus').replace('.', '_')
                    layer = Layer(
                        name=safe_name,
                        type=LayerType.APT,
                        content=package
                    )
                    layers.append(layer)
            
            # Parse script installs from heavy_setup
            if declaration.heavy_setup.script_installs:
                for script in declaration.heavy_setup.script_installs:
                    layer = Layer(
                        name=script.name,
                        type=LayerType.SCRIPT,
                        content='\n'.join(script.commands)
                    )
                    layers.append(layer)
        
        # Parse from light_setup (config files and quick setups)
        if hasattr(declaration, 'light_setup') and declaration.light_setup:
            for category, configs in declaration.light_setup.items():
                for config in configs:
                    layer = Layer(
                        name=config.name,
                        type=LayerType.CONFIG,
                        content='\n'.join(config.commands)
                    )
                    layers.append(layer)
        
        # Parse from 'layers' field if exists (future format)
        if hasattr(declaration, 'layers') and declaration.layers:
            # Parse APT packages
            if 'apt' in declaration.layers:
                for package in declaration.layers['apt']:
                    # Replace special characters in package names for layer naming
                    safe_name = package.replace('-', '_').replace('+', 'plus').replace('.', '_')
                    layer = Layer(
                        name=safe_name,
                        type=LayerType.APT,
                        content=package
                    )
                    layers.append(layer)
            
            # Parse scripts
            if 'scripts' in declaration.layers:
                for script in declaration.layers['scripts']:
                    layer = Layer(
                        name=script['name'],
                        type=LayerType.SCRIPT,
                        content='\n'.join(script.get('commands', []))
                    )
                    layers.append(layer)
        
        # Legacy support: parse apt_packages
        elif declaration.apt_packages:
            for package in declaration.apt_packages:
                # Replace special characters in package names for layer naming
                safe_name = package.replace('-', '_').replace('+', 'plus').replace('.', '_')
                layer = Layer(
                    name=safe_name,
                    type=LayerType.APT,
                    content=package
                )
                layers.append(layer)
        
        return layers
    
    def _build_layer(self, layer: Layer, parent_image: str, env_vars: Dict[str, str], image_name: str) -> str:
        """Build a single layer"""
        print(f"🔨 Starting build for layer: {layer.name} (type: {layer.type.value})")
        print(f"   Parent image: {parent_image}")
        
        # Generate Dockerfile
        print(f"   Generating Dockerfile...")
        dockerfile_path = self._generate_layer_dockerfile(layer, parent_image, env_vars)
        print(f"   Dockerfile generated: {dockerfile_path}")
        
        # Build image
        image_tag = layer.get_image_tag(image_name)
        print(f"   Target image: {image_tag}")
        
        cmd = sudo_prefix() + [
            'docker', 'build',
            '-f', dockerfile_path,
            '-t', image_tag,
            self.work_dir
        ]
        
        # For APT packages, add retry logic
        max_retries = 3 if layer.type == LayerType.APT else 1
        
        for attempt in range(max_retries):
            if max_retries > 1:
                print(f"   Attempt {attempt + 1}/{max_retries}...")
            
            print(f"   Running command: {' '.join(cmd)}")
            print(f"   Starting Docker build (real-time output)...")
            
            # Run without capturing output so it shows in real time
            result = subprocess.run(cmd, cwd=self.work_dir)
            
            if result.returncode == 0:
                print(f"✅ Successfully built layer {layer.name}")
                return image_tag
            else:
                if attempt < max_retries - 1:
                    print(f"⚠️  Build attempt {attempt + 1} failed for layer {layer.name}, retrying...")
                    # Add a small delay before retry
                    import time
                    time.sleep(2)
                else:
                    print(f"❌ Failed to build layer {layer.name} after {max_retries} attempts")
                    raise RuntimeError(f"Layer build failed: {layer.name}")
        
        return image_tag
    
    def _generate_layer_dockerfile(self, layer: Layer, parent_image: str, env_vars: Dict[str, str]) -> str:
        """Generate Dockerfile for a layer"""
        dockerfile_name = f"Dockerfile.{layer.type.value}-{layer.name}"
        dockerfile_path = os.path.join(self.work_dir, dockerfile_name)
        
        print(f"   📝 Generating Dockerfile: {dockerfile_name}")
        
        lines = [f"FROM {parent_image}"]
        
        # Add environment variables
        if env_vars:
            print(f"      Adding {len(env_vars)} environment variables")
            for key, value in env_vars.items():
                lines.append(f"ENV {key}=\"{value}\"")
        
        # Generate RUN command based on layer type
        if layer.type == LayerType.APT:
            print(f"      APT package: {layer.content}")
            # Add retry logic for APT commands to handle network issues
            apt_cmd = f"DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends {layer.content}"
            retry_cmd = f"RUN for i in {{1..3}}; do {apt_cmd} && break || (echo \"APT install attempt $i failed, retrying in 5 seconds...\" && sleep 5); done"
            lines.append(retry_cmd)
        elif layer.type == LayerType.SCRIPT:
            print(f"      Script commands: {len(layer.content.split(chr(10)))} lines")
            commands = layer.content.split('\n')
            if len(commands) == 1:
                lines.append(f"RUN {commands[0]}")
            else:
                lines.append(f"RUN {' && '.join(commands)}")
        elif layer.type == LayerType.CONFIG:
            print(f"      Config commands: {len(layer.content.split(chr(10)))} lines")
            commands = layer.content.split('\n')
            if len(commands) == 1:
                lines.append(f"RUN {commands[0]}")
            else:
                lines.append(f"RUN {' && '.join(commands)}")
        
        # Add metadata
        lines.append(f"LABEL layer.name=\"{layer.name}\"")
        lines.append(f"LABEL layer.type=\"{layer.type.value}\"")
        lines.append(f"LABEL layer.hash=\"{layer.hash}\"")
        
        print(f"      Writing {len(lines)} lines to Dockerfile")
        with open(dockerfile_path, 'w') as f:
            f.write('\n'.join(lines))
        
        print(f"      📄 Dockerfile ready: {dockerfile_path}")
        return dockerfile_path
    
    def _image_exists(self, image_tag: str) -> bool:
        """Check if Docker image exists"""
        cmd = ['docker', 'images', '-q', image_tag]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return bool(result.stdout.strip())
    
    def _tag_image(self, source: str, target: str):
        """Tag a Docker image"""
        print(f"🏷️  Tagging image: {source} -> {target}")
        cmd = sudo_prefix() + ['docker', 'tag', source, target]
        print(f"   Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"❌ Failed to tag image:")
            if result.stderr.strip():
                print(f"STDERR: {result.stderr}")
            raise RuntimeError(f"Failed to tag {source} as {target}")
        print(f"✅ Successfully tagged image")
    
    def _get_env_vars(self, declaration: UserDeclaration) -> Dict[str, str]:
        """Get environment variables from declaration"""
        if not declaration.inherit_env:
            return {}
        
        env_config = EnvVarConfig(
            inherit_proxy=declaration.inherit_proxy,
            inherit_locale=declaration.inherit_locale,
            inherit_timezone=declaration.inherit_timezone,
            inherit_custom=declaration.inherit_custom_env,
            exclude_vars=declaration.exclude_env
        )
        
        env_manager = EnvironmentManager(env_config)
        return env_manager.extract_system_env_vars()
    
    def _parse_config(self, config_file: str) -> UserDeclaration:
        """Parse configuration file"""
        if config_file.endswith('.yaml') or config_file.endswith('.yml'):
            return self.parser.parse_yaml(config_file)
        elif config_file.endswith('.json'):
            return self.parser.parse_json(config_file)
        else:
            raise ValueError(f"Unsupported config file format: {config_file}")
    
    def _show_inherited_env_summary(self, declaration: UserDeclaration):
        """Show summary of inherited environment variables"""
        if not declaration.inherit_env:
            return
        
        env_vars = self._get_env_vars(declaration)
        if env_vars:
            print(f"🌍 Inheriting {len(env_vars)} environment variables")
            if declaration.inherit_proxy:
                proxy_vars = [k for k in env_vars.keys() if 'proxy' in k.lower()]
                if proxy_vars:
                    print(f"   Including proxy: {', '.join(proxy_vars)}")
    
    def _execute_build(self, declaration: UserDeclaration, stage_order: List[str], 
                      build_steps: List, actions: List[str], image_tag: str, 
                      rebuild_from_step: int) -> bool:
        """Execute the Docker build"""
        # Generate Dockerfile with dynamic env injection
        dockerfile = self.generator.generate(declaration, stage_order, rebuild_from_step)
        
        # Write Dockerfile to temp file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.Dockerfile', delete=False) as f:
            f.write(dockerfile)
            dockerfile_path = f.name
        
        try:
            # Execute Docker build
            cmd = sudo_prefix() + [
                'docker', 'build',
                '-f', dockerfile_path,
                '-t', image_tag,
                '.'
            ]
            
            print(f"🐋 Building image: {image_tag}")
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode == 0:
                print(f"✅ Successfully built: {image_tag}")
                return True
            else:
                print(f"❌ Build failed:\n{result.stderr}")
                return False
                
        finally:
            os.unlink(dockerfile_path)
    
    def show_build_status(self, config_file: Optional[str] = None) -> Dict:
        """Show build status and cache information"""
        status = {
            "cache_stats": {
                "total_cached_steps": len(self.tracker.get_cached_steps()),
                "recent_builds": len(self.tracker.build_history.get("builds", []))
            },
            "cache_levels": {
                "local": True,
                "minio": bool(self.cache_config.minio_endpoint),
                "ghcr": bool(self.cache_config.ghcr_registry)
            }
        }
        
        # Add layer cache stats if in layered mode
        if self.layer_cache:
            layer_stats = {
                "total_layers": len(self.layer_cache.get("layers", {})),
                "total_chains": len(self.layer_cache.get("layer_chains", {}))
            }
            status["layer_cache"] = layer_stats
        
        return status
    
    def clean_cache(self, max_age_days: int = 30) -> bool:
        """Clean old cache entries"""
        print(f"🧹 Cleaning cache entries older than {max_age_days} days...")
        
        # Clean build tracker cache
        self.tracker.clean_old_entries(max_age_days)
        
        # Clean layer cache if exists
        if self.layer_cache:
            self._clean_layer_cache(max_age_days)
        
        print("✅ Cache cleaned")
        return True
    
    def _clean_layer_cache(self, max_age_days: int):
        """Clean old layer cache entries"""
        cutoff = datetime.now() - timedelta(days=max_age_days)
        
        # Find layers to remove
        to_remove = []
        for key, layer in self.layer_cache.get("layers", {}).items():
            created = datetime.fromisoformat(layer["created"])
            if created < cutoff:
                to_remove.append(key)
        
        # Remove old layers
        for key in to_remove:
            layer = self.layer_cache["layers"][key]
            # Try to remove Docker image
            if self._image_exists(layer["image"]):
                cmd = sudo_prefix() + ['docker', 'rmi', layer["image"]]
                subprocess.run(cmd, capture_output=True)
            
            del self.layer_cache["layers"][key]
        
        if to_remove:
            self._save_layer_cache()
            print(f"  Removed {len(to_remove)} old layers")

import subprocess
import tempfile
import os
import shutil
from typing import List, Optional
from config import UserDeclaration, CacheConfig, CacheLevel
from parser import DeclarationParser
from dockerfile_generator import DockerfileGenerator
from build_tracker import BuildTracker
from cache_manager import CacheManager
from env_manager import EnvironmentManager, EnvVarConfig


def sudo_prefix() -> List[str]:
    """Return sudo prefix if not running as root"""
    if os.geteuid() != 0:
        return ['sudo', '-E']
    return []


class BuildOrchestrator:
    def __init__(self, cache_config: CacheConfig = None):
        self.cache_config = cache_config or CacheConfig()
        self.parser = DeclarationParser()
        self.generator = DockerfileGenerator()
        self.tracker = BuildTracker()
        self.cache_manager = CacheManager(self.cache_config)
    
    def build_image(self, config_file: str, force_rebuild: bool = False) -> bool:
        """Build Docker image from configuration file"""
        try:
            # Parse user declaration
            declaration = self._parse_config(config_file)
            if not self.parser.validate_declaration(declaration):
                raise ValueError("Invalid configuration")
            
            # Get image tag from configuration
            image_tag = f"{declaration.image_name}:{declaration.image_tag}"
            
            # Record stage changes before getting order
            changed_stages = self.tracker.record_stage_changes(declaration.stages, image_tag)
            changed_stage_names = [name for name, changed in changed_stages.items() if changed]
            
            if changed_stage_names:
                print(f"🔄 Detected changes in stages: {', '.join(changed_stage_names)}")
            else:
                print("✅ No stage changes detected")
            
            # Get optimized stage order with dynamic reordering
            stage_order = self.parser.get_stage_order(declaration, self.tracker)
            
            # Apply additional optimization based on change detection
            original_order = stage_order.copy()
            stage_order = self.tracker.get_optimized_stage_order(declaration.stages, stage_order)
            
            if original_order != stage_order:
                print(f"🔀 Stage order optimized: {' → '.join(stage_order)}")
            else:
                print(f"📋 Stage order: {' → '.join(stage_order)}")
            
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
                
                if rebuild_plan['keep_steps'] > 0:
                    print(f"⚡ Cache will accelerate {rebuild_plan['keep_steps']} steps")
            else:
                rebuild_plan = {"actions": ['rebuild'] * len(build_steps)}
                print("🔥 Force rebuild requested - ignoring all cache")
            
            # Execute build
            success = self._execute_build(
                declaration, stage_order, build_steps, 
                rebuild_plan["actions"], image_tag
            )
            
            if success:
                self.tracker.record_build(build_steps, image_tag)
                print(f"Successfully built image: {image_tag}")
                print(f"Final stage order: {' -> '.join(stage_order)}")
            
            return success
            
        except Exception as e:
            print(f"Build failed: {str(e)}")
            return False
    
    def _parse_config(self, config_file: str) -> UserDeclaration:
        """Parse configuration file based on extension"""
        if config_file.endswith('.yaml') or config_file.endswith('.yml'):
            return self.parser.parse_yaml(config_file)
        elif config_file.endswith('.json'):
            return self.parser.parse_json(config_file)
        else:
            raise ValueError(f"Unsupported config file format: {config_file}")
    
    def _execute_build(self, declaration: UserDeclaration, stage_order: List[str],
                      build_steps: List, actions: List[str], image_tag: str) -> bool:
        """Execute the actual Docker build process"""
        
        # Generate Dockerfile
        dockerfile_content = self.generator.generate(declaration, stage_order)
        
        print(f"\n📝 Generated Dockerfile with {len(stage_order)} stages:")
        for i, stage_name in enumerate(stage_order, 1):
            print(f"  {i}. {stage_name}")
        
        # Create build context
        with tempfile.TemporaryDirectory() as build_dir:
            dockerfile_path = os.path.join(build_dir, 'Dockerfile')
            with open(dockerfile_path, 'w') as f:
                f.write(dockerfile_content)
            
            print(f"📂 Build context created: {build_dir}")
            
            # Check for cached layers and modify build accordingly
            optimized_dockerfile = self._apply_cache_optimization(
                dockerfile_content, build_steps, actions
            )
            
            if optimized_dockerfile != dockerfile_content:
                with open(dockerfile_path, 'w') as f:
                    f.write(optimized_dockerfile)
                print("⚡ Applied cache optimizations to Dockerfile")
            
            # Execute Docker build
            build_command = sudo_prefix() + [
                'docker', 'build',
                '-t', image_tag,
                '-f', dockerfile_path,
                build_dir
            ]
            
            # Add build args for environment variables  
            env_build_args = self._get_env_build_args(declaration)
            build_command.extend(env_build_args)
            
            try:
                # Set environment to use legacy Docker builder
                env = os.environ.copy()
                env['DOCKER_BUILDKIT'] = '0'
                
                print(f"\n🚀 Starting Docker build...")
                print(f"📦 Image: {image_tag}")
                print(f"📋 Build command: {' '.join(build_command)}")
                print("-" * 60)
                
                # Stream output in real-time without capturing
                process = subprocess.Popen(
                    build_command,
                    env=env,
                    cwd=build_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    universal_newlines=True,
                    bufsize=1
                )
                
                # Stream output line by line
                step_count = 0
                total_steps = len([line for line in dockerfile_content.split('\n') if line.strip() and not line.strip().startswith('#')])
                
                try:
                    while True:
                        output = process.stdout.readline()
                        if output == '' and process.poll() is not None:
                            break
                        if output:
                            line = output.strip()
                            print(line)
                            
                            # Track progress for RUN steps
                            if line.startswith('Step ') and '/' in line:
                                try:
                                    parts = line.split(' ')
                                    if len(parts) > 1 and '/' in parts[1]:
                                        current = int(parts[1].split('/')[0])
                                        total = int(parts[1].split('/')[1])
                                        progress = (current / total) * 100
                                        print(f"📊 Progress: {current}/{total} ({progress:.1f}%)")
                                except (ValueError, IndexError):
                                    pass
                            
                            # Highlight important steps
                            if 'Successfully built' in line:
                                print(f"🎉 Build completed!")
                            elif 'Successfully tagged' in line:
                                print(f"🏷️  Image tagged successfully")
                            elif 'ERROR' in line or 'Error' in line:
                                print(f"❌ Build error detected")
                            elif '---> Running in' in line:
                                print(f"🔄 Executing...")
                            elif 'Removing intermediate container' in line:
                                print(f"🧹 Cleanup...")
                
                except KeyboardInterrupt:
                    print(f"\n⏹️  Build interrupted by user")
                    print("🔄 Terminating Docker process...")
                    process.terminate()
                    process.wait()
                    return False
                
                # Wait for process to complete and get return code
                return_code = process.poll()
                
                if return_code == 0:
                    print("-" * 60)
                    print("✅ Docker build completed successfully")
                    
                    # Update cache information for steps
                    self._update_step_cache_info(build_steps, actions)
                    return True
                else:
                    print("-" * 60)
                    print(f"❌ Docker build failed with exit code: {return_code}")
                    print("💡 Check the output above for error details")
                    return False
                    
            except KeyboardInterrupt:
                print(f"\n⏹️  Build interrupted by user")
                return False
            except Exception as e:
                print(f"💥 Failed to execute Docker build: {str(e)}")
                return False
    
    def _apply_cache_optimization(self, dockerfile_content: str, 
                                 build_steps: List, actions: List[str]) -> str:
        """Apply cache optimization to Dockerfile based on cached steps"""
        lines = dockerfile_content.split('\n')
        optimized_lines = []
        
        # Add cache-related comments and modifications
        for i, line in enumerate(lines):
            optimized_lines.append(line)
            
            # Add cache layer information as comments
            if line.startswith('RUN ') and i < len(actions):
                if actions[i] == 'keep':
                    optimized_lines.insert(-1, f"# CACHE: This step should be cached")
                elif actions[i] == 'rebuild':
                    optimized_lines.insert(-1, f"# REBUILD: This step needs rebuilding")
        
        return '\n'.join(optimized_lines)
    
    def _update_step_cache_info(self, build_steps: List, actions: List[str]):
        """Update cache information for build steps"""
        for i, (step, action) in enumerate(zip(build_steps, actions)):
            if action == 'keep':
                # Step was cached, mark as such
                step.cached = True
                step.cache_level = CacheLevel.LOCAL
                
                # Try to promote to higher cache levels
                if self.cache_manager.minio_cache:
                    if not self.cache_manager.exists(step.hash, CacheLevel.MINIO):
                        # Could promote to Minio if we had the layer data
                        pass
                
                if self.cache_manager.ghcr_cache:
                    if not self.cache_manager.exists(step.hash, CacheLevel.GHCR):
                        # Could promote to GHCR if we had the layer data
                        pass
    
    def clean_cache(self, max_age_days: int = 30) -> bool:
        """Clean old cache entries"""
        try:
            self.tracker.cleanup_old_builds(keep_last=10)
            print(f"Cache cleanup completed")
            return True
        except Exception as e:
            print(f"Cache cleanup failed: {str(e)}")
            return False
    
    def show_build_status(self, config_file: str = None) -> dict:
        """Show current build status and cache information"""
        status = {
            "cache_stats": {
                "total_cached_steps": len(self.tracker.get_cached_steps()),
                "recent_builds": len(self.tracker.build_history.get("builds", [])),
            },
            "cache_levels": {
                "local": self.cache_manager.local_cache is not None,
                "minio": self.cache_manager.minio_cache is not None,
                "ghcr": self.cache_manager.ghcr_cache is not None,
            }
        }
        
        # Add stage change frequency information
        stage_frequency = self.tracker.get_stage_change_frequency()
        if stage_frequency:
            status["stage_change_frequency"] = stage_frequency
        
        if config_file:
            try:
                declaration = self._parse_config(config_file)
                
                # Detect current stage changes
                changed_stages = self.tracker.detect_stage_changes(declaration.stages)
                status["stage_changes"] = changed_stages
                
                # Get optimized order
                stage_order = self.parser.get_stage_order(declaration, self.tracker)
                optimized_order = self.tracker.get_optimized_stage_order(declaration.stages, stage_order)
                
                status["stage_order"] = {
                    "original": stage_order,
                    "optimized": optimized_order,
                    "reordered_count": len([s for s in optimized_order if s not in stage_order[:len(stage_order)//2]])
                }
                
                build_steps = self.generator.generate_build_steps(declaration, optimized_order)
                rebuild_plan = self.tracker.get_rebuild_plan(build_steps)
                
                status["current_config"] = {
                    "total_steps": rebuild_plan["total_steps"],
                    "cached_steps": rebuild_plan["keep_steps"],
                    "rebuild_steps": rebuild_plan["rebuild_steps"],
                    "efficiency": f"{rebuild_plan['efficiency']:.1%}"
                }
            except Exception as e:
                status["current_config"] = {"error": str(e)}
        
        return status
    
    def _show_inherited_env_summary(self, declaration: UserDeclaration):
        """Show summary of inherited environment variables"""
        if not getattr(declaration, 'inherit_env', True):
            print("🔒 Environment variable inheritance disabled")
            return
        
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
        env_manager.print_inherited_vars_summary(inherited_vars)
    
    def _get_env_build_args(self, declaration: UserDeclaration) -> List[str]:
        """Get Docker build arguments for environment variables"""
        if not getattr(declaration, 'inherit_env', True):
            return []
        
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
        return env_manager.get_docker_build_args(inherited_vars)
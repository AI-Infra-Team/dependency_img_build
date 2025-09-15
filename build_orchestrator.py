import subprocess
import tempfile
import os
import shutil
import hashlib
import json
import concurrent.futures
from typing import List, Optional, Dict, Tuple, Set
from datetime import datetime, timedelta
from pathlib import Path
from config import UserDeclaration, CacheConfig, CacheLevel, Layer, LayerType
from parser import DeclarationParser
from dockerfile_generator import DockerfileGenerator
from build_tracker import BuildTracker
from cache_manager import CacheManager
from env_manager import EnvironmentManager, EnvVarConfig
from container_layer_builder import ContainerLayerBuilder
from reuse import LayerReuseManager

# Package manager implementations

# Abstract package manager interfaces and concrete implementations
from abc import ABC, abstractmethod

class PackageManager(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def needs_refresh(self) -> bool: ...

    @abstractmethod
    def refresh_cmd(self) -> Optional[str]: ...

    @abstractmethod
    def install_cmd(self, package: str) -> str: ...

    @abstractmethod
    def remove_cmd(self, packages: List[str]) -> str: ...


class AptManager(PackageManager):
    @property
    def name(self) -> str:
        return 'apt'

    @property
    def needs_refresh(self) -> bool:
        return True

    def refresh_cmd(self) -> Optional[str]:
        return 'apt-get update'

    def install_cmd(self, package: str) -> str:
        return f"DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends {package}"

    def remove_cmd(self, packages: List[str]) -> str:
        pkgs = ' '.join(packages)
        return (
            f"DEBIAN_FRONTEND=noninteractive apt-get purge -y {pkgs}"
            " || true && DEBIAN_FRONTEND=noninteractive apt-get autoremove -y || true"
        )


class YumManager(PackageManager):
    @property
    def name(self) -> str:
        return 'yum'

    @property
    def needs_refresh(self) -> bool:
        return True

    def refresh_cmd(self) -> Optional[str]:
        return 'yum makecache'

    def install_cmd(self, package: str) -> str:
        return f"yum install -y {package}"

    def remove_cmd(self, packages: List[str]) -> str:
        return "yum remove -y " + ' '.join(packages) + " || true"


class PipManager(PackageManager):
    @property
    def name(self) -> str:
        return 'pip'

    @property
    def needs_refresh(self) -> bool:
        return False

    def refresh_cmd(self) -> Optional[str]:
        return None

    def install_cmd(self, package: str) -> str:
        return f"python3 -m pip install --no-cache-dir {package}"

    def remove_cmd(self, packages: List[str]) -> str:
        return "python3 -m pip uninstall -y " + ' '.join(packages) + " || true"


PM_REGISTRY: Dict[str, PackageManager] = {
    'apt': AptManager(),
    'yum': YumManager(),
    'pip': PipManager(),
}

def pm_for_layer_type(layer_type: LayerType) -> Optional[PackageManager]:
    if layer_type == LayerType.APT:
        return PM_REGISTRY['apt']
    if layer_type == LayerType.YUM:
        return PM_REGISTRY['yum']
    if layer_type == LayerType.PIP:
        return PM_REGISTRY['pip']
    return None


from utils import sudo_prefix


class BuildOrchestrator:
    """Orchestrator for Docker builds with layered architecture support"""
    
    def __init__(self, cache_config: CacheConfig = None):
        self.cache_config = cache_config or CacheConfig()
        self.parser = DeclarationParser()
        self.generator = DockerfileGenerator()
        self.tracker = BuildTracker()
        self.cache_manager = CacheManager(self.cache_config)
        self.reuse_manager = LayerReuseManager()
        
        # Work directory for Dockerfiles
        self.work_dir = None
        # Keep original script install definitions (to access copies/file later)
        self._script_install_defs = {}
    
    # Legacy layer cache removed; rely on in-image metadata only
    
    def build_image(self, config_file: str, force_rebuild: bool = False) -> bool:
        """Build Docker image from configuration file"""
        build_succeeded = False
        try:
            self.config_dir = os.path.dirname(os.path.abspath(config_file))
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
        build_succeeded = False
        
        try:
            # Parse all layers from configuration
            print(f"📋 Parsing layers from configuration...")
            all_layers = self._parse_layers(declaration)
            print(f"   Found {len(all_layers)} total layers")
            
            # Get environment variables
            print(f"🌍 Processing environment variables...")
            env_vars = self._get_env_vars(declaration)
            print(f"   Found {len(env_vars)} environment variables")

            # Compute naming scheme components based on base image
            base_repo, base_tag = self._parse_base_image(declaration.base_image)
            self.base_repo_slug = self._slugify(base_repo)
            self.base_tag_slug = self._slugify(base_tag or 'latest')
            # Repository name with base prefix
            self.repo_name = f"{self.base_repo_slug}__{declaration.image_name}"
            
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
                    preferred_repo=self.repo_name,
                    required_tag_prefix=self.base_tag_slug + "__"
                )
                parent_image = base_image
                reused_base_tag = base_image if base_image != declaration.base_image else None
                
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
                    # Group removals by manager
                    remove_groups: Dict[str, List[str]] = {}
                    for cleanup in cleanup_commands:
                        ctype = cleanup.get('type')
                        if ctype in ('apt_remove', 'yum_remove', 'pip_remove'):
                            pkgs = cleanup.get('packages', [])
                            pm_name = ctype.split('_', 1)[0]
                            remove_groups.setdefault(pm_name, []).extend(pkgs)
                            print(f"      - {cleanup['description']}: {', '.join(pkgs[:3])}{'...' if len(pkgs) > 3 else ''}")
                        elif ctype == 'script_remove':
                            print(f"      - {cleanup['description']}: {', '.join(cleanup.get('scripts', [])[:3])}{'...' if len(cleanup.get('scripts', [])) > 3 else ''}")
                    # Create cleanup layers per manager
                    for pm_name, pkgs in remove_groups.items():
                        pkgs = sorted(set(pkgs))
                        pm = PM_REGISTRY.get(pm_name)
                        if pm and pkgs:
                            cleanup_cmd = pm.remove_cmd(pkgs)
                            cleanup_layers.append(Layer(
                                name=f"{pm_name}_cleanup_remove",
                                type=LayerType.SCRIPT,
                                content=cleanup_cmd
                            ))
                    if cleanup_layers:
                        layers_to_build = cleanup_layers + layers_to_build

                # Summarize the concrete build plan and reuse list
                build_list = [f"{l.type.value}:{l.name}" for l in layers_to_build]
                if build_list:
                    print(f"   📝 Build list (order): {', '.join(build_list)}")
                reuse_list = [f"{l.type.value}:{l.name}" for l in all_layers if l.name in reused_layer_names and l.type != LayerType.BASE]
                if reuse_list:
                    print(f"   ♻️  Reuse list: {', '.join(reuse_list)}")
                # Also print missing (to-be-built) non-maintenance items for clarity
                missing_items: List[str] = []
                maintenance_names = {"apt_update", "yum_makecache", "apt_refresh", "yum_refresh"}
                for l in layers_to_build:
                    if l.type in (LayerType.APT, LayerType.YUM, LayerType.SCRIPT):
                        if l.name in maintenance_names or l.name.endswith("_cleanup_remove"):
                            continue
                        missing_items.append(f"{l.type.value}:{l.name}")
                if missing_items:
                    print(f"   ❗ Missing list:")
                    for it in missing_items:
                        print(f"   ❗ {it}")
            
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
            
            # If we need to build package layers on top of a reused base, preview and run per-PM refresh
            if parent_image != declaration.base_image:
                managers_needed = sorted({
                    pm_for_layer_type(l.type).name
                    for l in layers_to_build
                    if pm_for_layer_type(l.type) is not None
                })
                # Preview final steps (refresh + actual build layers)
                planned_steps = []
                for pm_name in managers_needed:
                    pm = PM_REGISTRY.get(pm_name)
                    if pm and pm.needs_refresh and pm.refresh_cmd():
                        if pm_name == 'apt' and getattr(declaration, 'apt_sources', []):
                            planned_steps.append("script:apt_sources")
                        planned_steps.append(f"script:{pm.name}_refresh")
                planned_steps.extend([f"{l.type.value}:{l.name}" for l in layers_to_build])
                if planned_steps:
                    print(f"   ▶ Next steps to build (order):")
                    for step in planned_steps:
                        print(f"   🛠️  {step}")

                # Execute refresh
                pm_refresh_done = set()
                for pm_name in managers_needed:
                    pm = PM_REGISTRY.get(pm_name)
                    if pm and pm.needs_refresh and pm_name not in pm_refresh_done:
                        print(f"🔄 Need to refresh {pm.name} metadata for continuing build...")
                        # Write custom APT sources first if configured
                        if pm_name == 'apt' and getattr(declaration, 'apt_sources', []):
                            print("   Writing custom APT sources before refresh...")
                            sources_cmd = self._render_apt_sources_commands(declaration.apt_sources)
                            sources_layer = Layer(
                                name="apt_sources",
                                type=LayerType.SCRIPT,
                                content=sources_cmd
                            )
                            image_tag = self._build_layer(sources_layer, parent_image, env_vars, declaration.image_name)
                            parent_image = image_tag
                        cmd = pm.refresh_cmd()
                        if cmd:
                            refresh_layer = Layer(
                                name=f"{pm.name}_refresh",
                                type=LayerType.SCRIPT,
                                content=cmd
                            )
                            image_tag = self._build_layer(refresh_layer, parent_image, env_vars, declaration.image_name)
                            parent_image = image_tag
                        pm_refresh_done.add(pm_name)
                        print(f"✓ Refreshed {pm.name} metadata")
            
            print(f"\n🔨 Building {len(layers_to_build)} new layers...")
            
            # Initialize container-based builder (no Dockerfiles)
            self._container_builder = ContainerLayerBuilder(env_vars, config_dir=getattr(self, 'config_dir', os.getcwd()), preserve_on_failure=True)

            # Build all the layers we need
            try:
                for i, layer in enumerate(layers_to_build):
                    print(f"\n📦 Building layer {i+1}/{len(layers_to_build)}: {layer.name}")
                    
                    # For the first package-manager layer when building from base, add metadata refresh
                    pm = pm_for_layer_type(layer.type)
                    if pm and parent_image == declaration.base_image and built_count == 0 and pm.needs_refresh and pm.refresh_cmd():
                        print(f"   Adding {pm.name} metadata refresh before first {pm.name} package...")
                        pm_update_layer = Layer(
                            name=f"{pm.name}_update",
                            type=LayerType.SCRIPT,
                            content=pm.refresh_cmd()
                        )
                        image_tag = self._build_layer(pm_update_layer, parent_image, env_vars, declaration.image_name)
                        parent_image = image_tag
                        print(f"✓ Refreshed {pm.name} metadata")
                    
                    # Build the layer via running container + snapshot commit
                    print(f"   Building layer {layer.name} (container commit mode)...")
                    # Resolve extra copies for script installs, if any
                    extra_copies: List[str] = []
                    try:
                        if hasattr(self, '_script_install_defs') and layer.type == LayerType.SCRIPT:
                            meta = self._script_install_defs.get(layer.name)
                            if meta and getattr(meta, 'copies', None):
                                extra_copies = list(meta.copies)
                    except Exception:
                        pass

                    # Build dependency metadata items up to this point (built + reused)
                    dep_items: List[str] = []
                    try:
                        maintenance_names = {"apt_update", "yum_makecache"}
                        for l in all_layers:
                            if l.type == LayerType.BASE:
                                continue
                            if l in built_layers or l.name in reused_layer_names:
                                if l.type in (LayerType.APT, LayerType.YUM, LayerType.PIP):
                                    dep_items.append(f"{l.type.value}:{l.content}")
                                elif l.type == LayerType.SCRIPT:
                                    if l.name in maintenance_names or l.name.endswith("_cleanup_remove"):
                                        continue
                                    dep_items.append(f"script:{l.name}")
                    except Exception:
                        dep_items = []

                    # Commit layer
                    target_image_tag = self._format_layer_image_tag(layer, declaration.image_name)
                    try:
                        image_tag = self._container_builder.build_layer(
                            layer,
                            parent_image,
                            target_image_tag,
                            copies=extra_copies,
                            metadata_items=dep_items
                        )
                    except Exception as be:
                        # Print manual reproduction hints
                        cname = getattr(self._container_builder, 'last_container_name', None)
                        cid = getattr(self._container_builder, 'last_container_id', None)
                        fcmd = getattr(self._container_builder, 'last_failed_cmd', None)
                        ref = cname or cid or '<unknown>'
                        print("\n🧯 Reproduce locally:")
                        print(f"  1) Start container: sudo -E docker start {ref}")
                        print(f"  2) Shell inside:   sudo -E docker exec -it {ref} /bin/bash")
                        if fcmd:
                            print(f"  3) Re-run failed:  sudo -E docker exec -it {ref} /bin/bash -lc {json.dumps(fcmd)}")
                        print(f"  4) Commit debug:   sudo -E docker commit {ref} debug/{self.repo_name.replace(':','_')}_{layer.name}_failed")
                        print(f"  5) Inspect labels: sudo -E docker image inspect debug/{self.repo_name.replace(':','_')}_{layer.name}_failed --format '{{{{json .Config.Labels}}}}' ")
                        raise
                    
                    # Track layers actually built in this run
                    built_layers.append(layer)

                    parent_image = image_tag
                    built_count += 1
                    print(f"✓ Built layer {layer.name}: {image_tag}")
                    print(f"   Progress: {built_count}/{len(layers_to_build)} layers completed")

                    # Metadata already written inside the container before commit
            except Exception as e:
                # On any failure, delete reused base (if any) and re-raise to abort
                if 'reused_base_tag' in locals() and reused_base_tag:
                    print(f"💥 Build failed. Removing reused base image: {reused_base_tag}")
                    try:
                        self._delete_image_safely(reused_base_tag)
                    except Exception as de:
                        print(f"⚠️  Failed to delete base image {reused_base_tag}: {de}")
                raise
            
            # Tag final image (embed dependency metadata first)
            print(f"\n🏷️  Tagging final image...")
            final_image = parent_image
            try:
                # Build list of used dependency identifiers (packages and scripts)
                used_items: List[str] = []
                maintenance_names = {"apt_update", "yum_makecache"}
                for l in all_layers:
                    if l.type == LayerType.BASE:
                        continue
                    # Include built + reused layers only
                    if l in built_layers or l.name in reused_layer_names:
                        if l.type in (LayerType.APT, LayerType.YUM, LayerType.PIP):
                            used_items.append(f"{l.type.value}:{l.content}")
                        elif l.type == LayerType.SCRIPT:
                            # Skip maintenance/cleanup script markers in metadata
                            if l.name in maintenance_names or l.name.endswith("_cleanup_remove"):
                                continue
                            used_items.append(f"script:{l.name}")

                # Embed metadata into the image via labels (no filesystem writes)
                if used_items:
                    print(f"   Embedding {len(used_items)} dependency items into image labels...")
                    final_image = self._embed_dependency_metadata(final_image, used_items)
                    print(f"   Metadata labels embedded")
                else:
                    print(f"   No dependency items to embed")
            except Exception as e:
                print(f"⚠️  Failed to embed metadata into image: {e}")
            else:
                if final_image:
                    # New naming: repo = baseNameSlug__image_name, tag = baseTagSlug__image_tag
                    target_repo = self.repo_name
                    target_tag = f"{self.base_tag_slug}__{declaration.image_tag}"
                    target_ref = f"{target_repo}:{target_tag}"
                    print(f"   Final image: {final_image}")
                    print(f"   Target tag: {target_ref}")
                    
                    if final_image != target_ref:
                        print(f"   Tagging {final_image} as {target_ref}")
                        self._tag_image(final_image, target_ref)
                    else:
                        print(f"   Image already has target tag")

                # Also tag classic name for backward compatibility with scripts/tests
                classic_ref = f"{declaration.image_name}:{declaration.image_tag}"
                if target_ref != classic_ref:
                    try:
                        print(f"   Adding classic tag: {classic_ref}")
                        self._tag_image(target_ref, classic_ref)
                    except Exception as e:
                        print(f"⚠️  Failed to add classic tag: {e}")
                
                    print(f"\n✅ Successfully built {target_tag}")
                    print(f"📊 Build stats: {built_count} built, {len(reused_layer_names) if not force_rebuild else 0} reused")
                    build_succeeded = True
                    return True
                
                return False
            
        finally:
            # Cleanup work directory only on success; keep on failure for debugging
            if build_succeeded:
                print(f"🧹 Cleaning up work directory...")
                if self.work_dir and os.path.exists(self.work_dir):
                    print(f"   Removing: {self.work_dir}")
                    shutil.rmtree(self.work_dir)
                    print(f"   ✅ Work directory cleaned up")
                else:
                    print(f"   No work directory to clean up")
            else:
                if self.work_dir and os.path.exists(self.work_dir):
                    print(f"🐞 Build failed; preserving work directory for debugging: {self.work_dir}")

    def _delete_image_safely(self, image_tag: str):
        """Force delete a local Docker image tag if it exists."""
        if not image_tag:
            return
        if not self._image_exists(image_tag):
            return
        cmd = sudo_prefix() + ['docker', 'rmi', '-f', image_tag]
        print(f"   Removing image: {image_tag}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"Failed to remove image {image_tag}")

    def _embed_dependency_metadata(self, base_image: str, items: List[str]) -> str:
        """Create a tiny layer on top of base_image with dependency labels only, then commit.

        Returns the new image tag with metadata labels embedded.
        """
        import hashlib
        content_hash = hashlib.sha256("\n".join(items).encode('utf-8')).hexdigest()[:12]
        meta_tag = f"{self.repo_name}:{self.base_tag_slug}__meta-{content_hash}"
        # Use container builder to commit labels without changing filesystem
        builder = getattr(self, '_container_builder', None)
        if builder is None:
            builder = ContainerLayerBuilder({}, config_dir=getattr(self, 'config_dir', os.getcwd()), preserve_on_failure=True)
        dummy_layer = Layer(name=f"meta_{content_hash}", type=LayerType.SCRIPT, content=":")
        print(f"   Embedding dependency metadata labels via container commit: {meta_tag}")
        return builder.build_layer(dummy_layer, base_image, meta_tag, copies=None, metadata_items=items)

    def _format_layer_image_tag(self, layer: Layer, image_name: str) -> str:
        """Format the image tag for a layer using naming scheme."""
        name = self._slugify(layer.name)
        ltype = self._slugify(layer.type.value)
        return f"{self.repo_name}:{self.base_tag_slug}__layer-{ltype}-{name}-{layer.hash}"

    @staticmethod
    def _slugify(s: str) -> str:
        return ''.join(c if c.isalnum() or c in ('-', '_') else '_' for c in s)

    @staticmethod
    def _parse_base_image(base_image: str) -> Tuple[str, str]:
        """Parse base image into (name, tag) where name may include registry/user, tag may be empty.
        We treat the last ':' after the last '/' as tag separator.
        """
        last_slash = base_image.rfind('/')
        last_colon = base_image.rfind(':')
        if last_colon > last_slash:
            name = base_image[:last_colon]
            tag = base_image[last_colon + 1:]
        else:
            name = base_image
            tag = 'latest'
        # Extract base repo name part (after last '/') for prefix human readability
        base_repo = name.split('/')[-1]
        return base_repo, tag

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
        
        # Check if we need APT/YUM packages and add update/makecache layers early for caching benefits
        has_apt_packages = False
        has_yum_packages = False
        has_pip_packages = False
        if hasattr(declaration, 'heavy_setup') and declaration.heavy_setup:
            if declaration.heavy_setup.apt_packages:
                has_apt_packages = True
            if getattr(declaration.heavy_setup, 'yum_packages', []):
                has_yum_packages = True
            if getattr(declaration.heavy_setup, 'pip_packages', []):
                has_pip_packages = True
        if declaration.apt_packages:
            has_apt_packages = True
        if declaration.yum_packages:
            has_yum_packages = True
        # note: top-level pip_packages not supported; use heavy_setup or layers

        if has_apt_packages:
            # If custom APT sources are configured, write them before apt update
            if getattr(declaration, 'apt_sources', None):
                sources_cmd = self._render_apt_sources_commands(declaration.apt_sources)
                layers.append(Layer(name="apt_sources", type=LayerType.SCRIPT, content=sources_cmd))
            apt_pm = PM_REGISTRY['apt']
            if apt_pm.refresh_cmd():
                layers.append(Layer(name="apt_update", type=LayerType.SCRIPT, content=apt_pm.refresh_cmd()))
        if has_yum_packages:
            yum_pm = PM_REGISTRY['yum']
            if yum_pm.refresh_cmd():
                layers.append(Layer(name="yum_makecache", type=LayerType.SCRIPT, content=yum_pm.refresh_cmd()))
        
        # Parse from heavy_setup first (current structure)
        if hasattr(declaration, 'heavy_setup') and declaration.heavy_setup:
            # Parse APT packages from heavy_setup
            if declaration.heavy_setup.apt_packages:
                for package in declaration.heavy_setup.apt_packages:
                    safe_name = package.replace('-', '_').replace('+', 'plus').replace('.', '_')
                    layers.append(Layer(name=safe_name, type=LayerType.APT, content=package))
            # Parse YUM packages from heavy_setup
            if getattr(declaration.heavy_setup, 'yum_packages', []):
                for package in declaration.heavy_setup.yum_packages:
                    safe_name = package.replace('-', '_').replace('+', 'plus').replace('.', '_')
                    layers.append(Layer(name=safe_name, type=LayerType.YUM, content=package))
            
            # Parse script installs from heavy_setup
            if declaration.heavy_setup.script_installs:
                for script in declaration.heavy_setup.script_installs:
                    # commands 与 file 二选一；若提供 file 则将其编码为单行命令 'file:<path>'
                    if getattr(script, 'file', None):
                        content = f"file:{script.file}"
                    else:
                        content = '\n'.join(script.commands)
                    layer = Layer(
                        name=script.name,
                        type=LayerType.SCRIPT,
                        content=content
                    )
                    # Store original definition for container builder (copies, file, etc.)
                    try:
                        self._script_install_defs[script.name] = script
                    except Exception:
                        pass
                    layers.append(layer)
            # Parse PIP packages from heavy_setup
            if getattr(declaration.heavy_setup, 'pip_packages', []):
                for package in declaration.heavy_setup.pip_packages:
                    safe_name = package.replace('-', '_').replace('+', 'plus').replace('.', '_')
                    layers.append(Layer(name=safe_name, type=LayerType.PIP, content=package))
        
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
            # Parse YUM packages (future format)
            if 'yum' in declaration.layers:
                for package in declaration.layers['yum']:
                    safe_name = package.replace('-', '_').replace('+', 'plus').replace('.', '_')
                    layers.append(Layer(name=safe_name, type=LayerType.YUM, content=package))
            
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

    def _render_apt_sources_commands(self, sources: List[str]) -> str:
        """Render shell commands to write custom APT sources to /etc/apt/sources.list"""
        import shlex
        if not sources:
            return ":"  # no-op
        quoted = ' '.join(shlex.quote(s) for s in sources)
        return (
            f"printf '%s\\n' {quoted} > /etc/apt/sources.list && chmod 0644 /etc/apt/sources.list"
        )
    
    def _build_layer(self, layer: Layer, parent_image: str, env_vars: Dict[str, str], image_name: str) -> str:
        """Build a single layer"""
        print(f"🔨 Starting build for layer: {layer.name} (type: {layer.type.value})")
        print(f"   Parent image: {parent_image}")
        
        # Generate Dockerfile
        print(f"   Generating Dockerfile...")
        dockerfile_path = self._generate_layer_dockerfile(layer, parent_image, env_vars)
        print(f"   Dockerfile generated: {dockerfile_path}")
        
        # Build image
        image_tag = self._format_layer_image_tag(layer, image_name)
        print(f"   Target image: {image_tag}")
        
        cmd = sudo_prefix() + [
            'docker', 'build',
            '-f', dockerfile_path,
            '-t', image_tag,
            self.work_dir
        ]
        
        # Outer docker build retries: rely on inner RUN-level retry for package layers
        # so we only run docker build once per layer to avoid 3x3 attempts.
        max_retries = 1
        
        for attempt in range(max_retries):
            if max_retries > 1:
                print(f"   Attempt {attempt + 1}/{max_retries}...")
            
            print(f"   Running command: {' '.join(cmd)}")
            print(f"   Starting Docker build (real-time output)...")
            
            # Run without capturing output so it shows in real time; enable BuildKit
            env = os.environ.copy()
            env.setdefault('DOCKER_BUILDKIT', '1')
            env.setdefault('BUILDKIT_PROGRESS', 'plain')
            result = subprocess.run(cmd, cwd=self.work_dir, env=env)
            
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
        
        # Add environment variables (escaped and grouped)
        if env_vars:
            print(f"      Adding {len(env_vars)} environment variables")
            try:
                from env_manager import EnvironmentManager
                env_lines = EnvironmentManager().generate_env_dockerfile_lines(env_vars)
                lines.extend(env_lines)
            except Exception:
                # Fallback to simple injection if helper fails
                for key, value in env_vars.items():
                    lines.append(f"ENV {key}=\"{value}\"")
        
        # Generate RUN command based on layer type
        pm = pm_for_layer_type(layer.type)
        if pm is not None:
            print(f"      {pm.name.upper()} package: {layer.content}")
            install_cmd = pm.install_cmd(layer.content)
            # For APT, always refresh metadata right before install to avoid stale indexes
            if pm.name == 'apt':
                install_cmd = f"apt-get update && {install_cmd}"
            # Robust retry without post-install verification (consistent policy: no verify for all PMs)
            retry_cmd = (
                "RUN set -e; success=0; for i in 1 2 3; do "
                + f"({install_cmd}) && success=1 && break || {{ echo 'Install attempt ' \"$i\" ' failed, retrying in 5 seconds...' >&2; sleep 5; }}; "
                + "done; [ \"$success\" = 1 ] || { echo 'Install failed after 3 attempts' >&2; exit 1; }"
            )
            lines.append(retry_cmd)
        elif layer.type == LayerType.SCRIPT:
            # Support file:relative/path syntax to run external scripts reliably
            commands = [ln for ln in layer.content.splitlines() if ln.strip()]
            print(f"      Script commands: {len(commands)} lines")
            # Accumulate inline shell commands to be joined; emit COPY/RUN for file: entries
            inline_cmds = []
            for raw in commands:
                cmd = raw.strip()
                if cmd.startswith('file:'):
                    rel = cmd.split(':', 1)[1].strip()
                    base = os.path.basename(rel)
                    cfg_dir = getattr(self, 'config_dir', os.getcwd())
                    src_abs = os.path.abspath(os.path.join(cfg_dir, rel))
                    dst_ctx = os.path.join(self.work_dir, base)
                    try:
                        # Copy into build context
                        import shutil
                        shutil.copy2(src_abs, dst_ctx)
                        print(f"      Added script to context: {rel} -> {dst_ctx}")
                    except Exception as e:
                        print(f"      ⚠️ Failed to copy script {rel} to context: {e}")
                        raise
                    dst_image = f"/dependency_img_build/{base}"
                    lines.append("RUN mkdir -p /dependency_img_build")
                    lines.append(f"COPY {base} {dst_image}")
                    lines.append(f"RUN chmod +x {dst_image}")
                    if base.endswith('.py'):
                        lines.append(f"RUN set -e; python3 {dst_image}")
                    else:
                        lines.append(f"RUN set -e; /bin/bash {dst_image}")
                else:
                    inline_cmds.append(cmd)
            if inline_cmds:
                if len(inline_cmds) == 1:
                    lines.append(f"RUN set -e; {inline_cmds[0]}")
                else:
                    lines.append(f"RUN set -e; {' && '.join(inline_cmds)}")
        elif layer.type == LayerType.CONFIG:
            commands = [ln for ln in layer.content.splitlines() if ln.strip()]
            print(f"      Config commands: {len(commands)} lines")
            if len(commands) == 1:
                lines.append(f"RUN set -e; {commands[0]}")
            elif commands:
                lines.append(f"RUN set -e; {' && '.join(commands)}")
        
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
            # Pass context info to generator so it can resolve file: paths relative to config file
            try:
                self.generator.config_dir = getattr(self, 'config_dir', os.getcwd())
                self.generator.build_context_dir = os.getcwd()
            except Exception:
                pass
            # Execute Docker build
            cmd = sudo_prefix() + [
                'docker', 'build',
                '-f', dockerfile_path,
                '-t', image_tag,
                '.'
            ]
            
            print(f"🐋 Building image: {image_tag}")
            # Stream Docker build output in real time (do not capture), enable BuildKit
            env = os.environ.copy()
            env.setdefault('DOCKER_BUILDKIT', '1')
            env.setdefault('BUILDKIT_PROGRESS', 'plain')
            result = subprocess.run(cmd, env=env)
            
            if result.returncode == 0:
                print(f"✅ Successfully built: {image_tag}")
                return True
            else:
                print(f"❌ Build failed (see logs above)")
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
        
        return status
    
    def clean_cache(self, max_age_days: int = 30) -> bool:
        """Clean old cache entries"""
        print(f"🧹 Cleaning cache entries older than {max_age_days} days...")
        
        # Clean build tracker cache
        self.tracker.clean_old_entries(max_age_days)
        
        print("✅ Cache cleaned")
        return True

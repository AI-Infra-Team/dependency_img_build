"""
Layer reuse logic using set intersection approach.
Find the image with maximum common packages, regardless of order.
"""

import os
import json
import subprocess
import logging
from typing import List, Dict, Optional, Tuple, Set
from datetime import datetime
from config import Layer, LayerType


class LayerReuseManager:
    """Manages layer reuse with set intersection approach"""
    
    def __init__(self, cache_file: str = "layers_cache.json"):
        self.cache_file = cache_file
        self.cache = self._load_cache()
        self._ensure_cache_structure()
        
    def _load_cache(self) -> Dict:
        """Load layer cache from file"""
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logging.warning(f"Failed to load cache: {e}")
        
        return {
            "layers": {},
            "image_sets": {},  # Track the SET of layers for each image
            "metadata": {
                "created": datetime.now().isoformat(),
                "version": "4.0"
            }
        }
    
    def _ensure_cache_structure(self):
        """Ensure cache has required structure and migrate old data"""
        if "image_sets" not in self.cache:
            self.cache["image_sets"] = {}
            
        # Migrate existing layer chains to image_sets
        if "layer_chains" in self.cache:
            logging.info("🔄 Migrating old cache format...")
            for image_tag, chain_data in self.cache.get("layer_chains", {}).items():
                if image_tag not in self.cache["image_sets"]:
                    # Extract packages from the chain
                    packages = []
                    for layer_image in chain_data.get("layers", []):
                        # Try to find this layer in the layers cache
                        for layer_key, layer_data in self.cache.get("layers", {}).items():
                            if layer_data.get("image") == layer_image:
                                if layer_data.get("type") in ["apt", "yum"]:
                                    packages.append(f"{layer_data['type']}:{layer_data['content']}")
                    
                    if packages:
                        self.cache["image_sets"][image_tag] = {
                            "packages": packages,
                            "migrated": True,
                            "created": chain_data.get("created", datetime.now().isoformat())
                        }
        
        # Also check for any docker images that exist but aren't in cache
        self._discover_existing_images()
    
    def save_cache(self):
        """Save layer cache to file"""
        print(f"💾 Saving cache to {self.cache_file}...")
        self.cache["metadata"]["last_updated"] = datetime.now().isoformat()
        with open(self.cache_file, 'w') as f:
            json.dump(self.cache, f, indent=2)
        print(f"   Cache saved successfully")
    
    def find_optimal_base(self, target_layers: List[Layer], preferred_repo: Optional[str] = None) -> Tuple[str, Set[str], List[Layer], List[Dict]]:
        """
        Find the optimal base image using SET INTERSECTION.
        
        Key insight: Find the image with most packages already built!
        
        Returns:
            - base_image: The best base image to start from
            - reused_layers: Set of layer names that are already in the base
            - layers_to_build: List of layers that need to be built
            - cleanup_commands: List of cleanup command descriptors for removing extra dependencies
        """
        logging.info("🔍 Finding optimal reuse strategy (set intersection)...")
        
        # Build target set (what we want)
        target_set = set()
        target_layer_map = {}  # Map from identifier to layer object
        all_target_layers = []  # Keep ordered list for building
        
        for layer in target_layers:
            if layer.type == LayerType.BASE:
                continue
            
            # For packages, track them in the set
            if layer.type in [LayerType.APT, LayerType.YUM]:
                layer_id = f"{layer.type.value}:{layer.content}"
                target_set.add(layer_id)
                target_layer_map[layer_id] = layer
            # For scripts, use name as identifier (ignore content changes unless name changes)
            elif layer.type == LayerType.SCRIPT:
                layer_id = f"script:{layer.name}"
                target_set.add(layer_id)
                target_layer_map[layer_id] = layer
            
            # Keep all layers for building
            all_target_layers.append(layer)
        
        logging.info(f"📋 Target: {len([i for i in target_set if not i.startswith('script:')])} packages, {len([i for i in target_set if i.startswith('script:')])} scripts, {len(all_target_layers)} total layers")
        
        # CRITICAL: First scan ALL existing Docker images
        logging.info("🐳 Scanning existing Docker images...")
        self._scan_all_docker_images()
        
        # Also ensure we have the latest cache
        self.save_cache()
        
        best_image = None
        best_intersection = set()
        best_score = -10000
        best_missing = set()
        
        # Check each cached image
        logging.info("📦 Evaluating cached images...")
        
        # Build image_sets from layers cache format
        image_sets = self._build_image_sets_from_layers()
        
        logging.info(f"Found {len(image_sets)} cached image sets")
        for tag in list(image_sets.keys())[:3]:  # Show first 3 for debugging
            logging.info(f"  {tag}: {len(image_sets[tag].get('packages', []))} packages")
        
        # For debugging, let's test a few recent images manually
        test_images = [
            'ubuntu22-dev:layer-script-apt_update-d629c545',
            'ubuntu22-dev:layer-apt-openssh_server-54dc603d',
            'ubuntu22-dev:layer-apt-sudo-ca420d1c'
        ]
        
        for img in test_images:
            exists = self._image_exists(img)
            logging.info(f"Manual check: {img} -> {'EXISTS' if exists else 'NOT FOUND'}")
        
        for image_tag, image_data in image_sets.items():
            # If restricting to a repository (e.g., only reuse same project images)
            if preferred_repo:
                repo = image_tag.split(':', 1)[0]
                if repo != preferred_repo:
                    continue
            # Check if image exists
            if not self._image_exists(image_tag):
                logging.info(f"Skipping {image_tag} - image does not exist")
                continue
            
            cached_set = set(image_data.get("packages", []))
            
            # Skip empty sets
            if not cached_set:
                logging.info(f"Skipping {image_tag} - empty package set")
                continue
            
            logging.info(f"=== Evaluating Image: {image_tag} ===")
            
            # Calculate intersection (what we can reuse)
            intersection = target_set & cached_set
            
            # Calculate difference (what we need to build)
            missing = target_set - cached_set
            
            # Calculate extra (what's in the image but we don't need)
            extra = cached_set - target_set
            
            logging.info(f"  Target set: {target_set}")
            logging.info(f"  Cached set: {cached_set}")
            logging.info(f"  Intersection: {intersection}")
            logging.info(f"  Missing: {missing}")
            logging.info(f"  Extra: {extra}")
            
            # Analyze extra dependencies strategy
            if extra:
                apt_extra = {item for item in extra if item.startswith('apt:')}
                script_extra = {item for item in extra if item.startswith('script:')}
                
                if apt_extra:
                    logging.warning(f"⚠️  Image contains {len(apt_extra)} extra APT packages that we don't need:")
                    for pkg in sorted(apt_extra):
                        logging.warning(f"     - {pkg}")
                    logging.warning(f"     Strategy: APT packages can be removed if needed, but will be kept for compatibility")
                
                if script_extra:
                    logging.warning(f"⚠️  Image contains {len(script_extra)} extra scripts that we don't need:")
                    for script in sorted(script_extra):
                        logging.warning(f"     - {script}")
                    logging.warning(f"     Strategy: Scripts cannot be safely removed from existing images, keeping them")
                    logging.warning(f"     Note: Consider updating your configuration to include these scripts if they're important")
            
            # Score: We want maximum intersection, minimum missing
            # Intersection is most valuable (each reusable item = +100 points)
            # Missing items have higher cost (each missing item = -50 points)  
            # Extra items have minimal cost (each extra item = -0.01 points)
            #
            # Strategy for extra dependencies:
            # - APT packages: Keep them (removal could break system stability)
            # - Scripts: Keep them (script results cannot be safely rolled back)
            score = len(intersection) * 100 - len(missing) * 50 - len(extra) * 0.01
            
            # Special case: if image has everything we need (or more), use it!
            if len(missing) == 0:
                score += 10000  # Huge bonus for complete match
                
            logging.debug(f"  📦 {image_tag[-30:]}:")  # Show last 30 chars of tag
            logging.debug(f"     Intersection: {len(intersection)} packages")
            logging.debug(f"     Missing: {len(missing)} packages") 
            logging.debug(f"     Extra: {len(extra)} packages")
            logging.debug(f"     Score: {score:.1f}")
            
            if score > best_score:
                best_score = score
                best_image = image_tag
                best_intersection = intersection
                best_missing = missing
        
        # Check if we evaluated any images
        if best_score == -10000:
            logging.warning("⚠️  No images were successfully evaluated - all were either missing or had empty package sets")
        
        # Determine what to build and what to clean up
        layers_to_build = []
        reused_layer_names = set()
        cleanup_commands = []
        best_extra = set()
        
        if best_image and len(best_intersection) > 0:
            logging.info(f"✅ Best base: {best_image[-40:]}")
            packages_reused = len([i for i in best_intersection if not i.startswith('script:')])
            scripts_reused = len([i for i in best_intersection if i.startswith('script:')])
            logging.info(f"   Reusing {packages_reused} packages, {scripts_reused} scripts")
            
            # Calculate what's extra in the base image (for potential cleanup)
            cached_set = set(image_sets.get(best_image, {}).get("packages", []))
            best_extra = cached_set - target_set
            
            # Generate cleanup commands for extra packages (only APT packages for now)
            if best_extra:
                cleanup_commands = self.generate_cleanup_commands(best_extra)
            
            # Show what we're keeping that we don't need
            if best_missing == 0 and len(best_intersection) < len([item for item_set in self._get_cached_items(best_image) for item in item_set]):
                logging.info(f"   📋 Note: Base image contains additional dependencies that will be kept")
                logging.info(f"   📋 Strategy: Extra APT packages kept for stability, extra scripts kept for safety")
            
            # If we have everything we need, just reuse the image!
            if len(best_missing) == 0:
                logging.info("   🎉 Image has everything we need!")
                # Only need to build configs (scripts can also be reused)
                for layer in all_target_layers:
                    if layer.type in [LayerType.CONFIG]:
                        layers_to_build.append(layer)
                    elif layer.type in [LayerType.APT, LayerType.YUM, LayerType.SCRIPT]:
                        reused_layer_names.add(layer.name)
            else:
                # Mark what we're reusing
                for layer_id in best_intersection:
                    if layer_id in target_layer_map:
                        reused_layer_names.add(target_layer_map[layer_id].name)
                
                # Build what's missing
                for layer in all_target_layers:
                    if layer.type in [LayerType.CONFIG]:
                        # Always rebuild configs (they're usually quick)
                        layers_to_build.append(layer)
                    elif layer.type in [LayerType.APT, LayerType.YUM]:
                        # Only build if not in intersection
                        layer_id = f"{layer.type.value}:{layer.content}"
                        if layer_id not in best_intersection:
                            layers_to_build.append(layer)
                    elif layer.type == LayerType.SCRIPT:
                        # Only build if not in intersection (based on name)
                        layer_id = f"script:{layer.name}"
                        if layer_id not in best_intersection:
                            layers_to_build.append(layer)
        else:
            # No good base found
            logging.info("❌ No suitable base found, building from scratch")
            best_image = target_layers[0].content if target_layers else "ubuntu:22.04"
            layers_to_build = all_target_layers
        
        logging.info(f"📊 Final: Reuse {len(reused_layer_names)}, Build {len(layers_to_build)}")
        
        return best_image, reused_layer_names, layers_to_build, cleanup_commands
    
    def generate_cleanup_commands(self, extra_packages: Set[str]) -> List[str]:
        """Generate safe cleanup commands for removing extra packages"""
        cleanup_commands = []
        
        if not extra_packages:
            return cleanup_commands
            
        apt_packages = []
        script_names = []
        
        for item in extra_packages:
            if item.startswith('apt:'):
                package_name = item[4:]  # Remove 'apt:' prefix
                apt_packages.append(package_name)
            elif item.startswith('script:'):
                script_name = item[7:]  # Remove 'script:' prefix
                script_names.append(script_name)
        
        if apt_packages:
            cleanup_commands.append({
                'type': 'apt_remove',
                'packages': apt_packages,
                'description': f'Safe removal of {len(apt_packages)} extra APT packages'
            })
            
        if script_names:
            cleanup_commands.append({
                'type': 'script_remove',
                'scripts': script_names,
                'description': f'Removal of {len(script_names)} extra scripts (if remove_commands available)'
            })
            
        return cleanup_commands
    
    def _build_image_sets_from_layers(self):
        """Build image_sets format from layers cache for compatibility"""
        # First check if we already have image_sets in the cache
        existing_image_sets = self.cache.get("image_sets", {})
        if existing_image_sets:
            # Keep package format as "apt:package" to match target_set format
            # No conversion needed since target_set uses "apt:package" format
            return existing_image_sets
        
        # Fallback: build from layers if image_sets is not available
        image_sets = {}
        
        # Group layers by image and build package list for each image
        for layer_key, layer_data in self.cache.get("layers", {}).items():
            image_tag = layer_data.get("image")
            layer_type = layer_data.get("type")
            layer_content = layer_data.get("content")
            layer_name = layer_data.get("name")
            
            if not image_tag:
                continue
                
            if image_tag not in image_sets:
                image_sets[image_tag] = {"packages": set()}
            
            # Use the same format as target_set
            if layer_type in ["apt", "yum"]:
                package_id = f"{layer_type}:{layer_content}"
                image_sets[image_tag]["packages"].add(package_id)
            elif layer_type == "script" and layer_name:
                # For scripts, use name as identifier
                script_id = f"script:{layer_name}"
                image_sets[image_tag]["packages"].add(script_id)
        
        # Build cumulative package sets since each image contains all previous packages
        sorted_layers = []
        for layer_key, layer_data in self.cache.get("layers", {}).items():
            if layer_data.get("type") in ["apt", "yum", "script"]:
                sorted_layers.append(layer_data)
        
        # Sort by creation time
        sorted_layers.sort(key=lambda x: x.get("created", ""))
        
        # Build cumulative package sets
        cumulative_packages = set()
        image_sets_cumulative = {}
        
        for layer_data in sorted_layers:
            image_tag = layer_data.get("image")
            layer_type = layer_data.get("type")
            layer_content = layer_data.get("content")
            layer_name = layer_data.get("name")
            
            if image_tag and layer_type and (layer_content or layer_name):
                if layer_type in ["apt", "yum"]:
                    package_id = f"{layer_type}:{layer_content}"
                    cumulative_packages.add(package_id)
                elif layer_type == "script" and layer_name:
                    script_id = f"script:{layer_name}"
                    cumulative_packages.add(script_id)
                    
                image_sets_cumulative[image_tag] = {"packages": list(cumulative_packages.copy())}
        
        return image_sets_cumulative
    
    def cache_layer(self, layer: Layer, image_tag: str):
        """Cache a successfully built layer"""
        print(f"💾 Caching layer: {layer.name} -> {image_tag}")
        layer_key = self._get_layer_key(layer)
        
        self.cache["layers"][layer_key] = {
            "name": layer.name,
            "type": layer.type.value,
            "hash": layer.hash,
            "image": image_tag,
            "created": datetime.now().isoformat(),
            "content": layer.content
        }
        
        print(f"   Layer cached with key: {layer_key}")
        # Don't save here, let cache_built_image do it
    
    def cache_built_image(self, image_tag: str, layers: List[Layer]):
        """
        Cache the complete set of packages and scripts in this image.
        """
        print(f"💾 Caching built image: {image_tag}")
        print(f"   Processing {len(layers)} layers for caching...")
        
        # Build the set of packages and scripts (not configs)
        package_set = []
        all_items = []
        
        for layer in layers:
            if layer.type == LayerType.BASE:
                continue
                
            item_id = f"{layer.type.value}:{layer.content}"
            all_items.append(item_id)
            
            # Track packages and scripts in the set (not configs)
            if layer.type in [LayerType.APT, LayerType.YUM]:
                package_set.append(item_id)
            elif layer.type == LayerType.SCRIPT:
                # Use name for script caching, not content
                script_id = f"script:{layer.name}"
                package_set.append(script_id)
        
        # Store the image's package set
        self.cache["image_sets"][image_tag] = {
            "packages": package_set,
            "all_layers": all_items,
            "package_count": len([p for p in package_set if not p.startswith("script:")]),
            "script_count": len([p for p in package_set if p.startswith("script:")]),
            "total_count": len(all_items),
            "created": datetime.now().isoformat()
        }
        
        print(f"   Cached {len([p for p in package_set if not p.startswith('script:')])} packages, {len([p for p in package_set if p.startswith('script:')])} scripts, {len(all_items)} total layers")
        self.save_cache()
        logging.info(f"💾 Cached image {image_tag} with {len(package_set)} reusable items")
    
    def _scan_all_docker_images(self):
        """Scan ALL Docker images and intelligently infer their package contents"""
        try:
            # Get all ubuntu22-dev images
            cmd = ['docker', 'images', '--format', '{{.Repository}}:{{.Tag}}', 'ubuntu22-dev']
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            
            if result.returncode == 0:
                images = result.stdout.strip().split('\n')
                logging.debug(f"  Found {len(images)} ubuntu22-dev images")
                
                for image in images:
                    if not image or image == "ubuntu22-dev:<none>":
                        continue
                    
                    # Skip if already properly cached
                    if image in self.cache["image_sets"] and len(self.cache["image_sets"][image].get("packages", [])) > 0:
                        logging.debug(f"  ✓ Already cached: {image[-40:]} ({len(self.cache['image_sets'][image]['packages'])} packages)")
                        continue
                    
                    # For layer images, infer content from name and position
                    if ":layer-" in image:
                        packages = self._smart_infer_packages(image)
                        if packages:
                            self.cache["image_sets"][image] = {
                                "packages": packages,
                                "inferred": True,
                                "created": datetime.now().isoformat()
                            }
                            logging.debug(f"  🔍 Inferred: {image[-40:]} ({len(packages)} packages)")
                    
                    # For latest image, use all packages from layers
                    elif image == "ubuntu22-dev:latest":
                        packages = self._reconstruct_all_packages()
                        if packages:
                            self.cache["image_sets"][image] = {
                                "packages": packages,
                                "reconstructed": True,
                                "created": datetime.now().isoformat()
                            }
                            logging.debug(f"  📦 Latest image: {len(packages)} packages")
                            
        except Exception as e:
            logging.warning(f"Failed to scan images: {e}")
    
    def _smart_infer_packages(self, image_tag: str) -> List[str]:
        """
        Smartly infer what packages are in an image based on its name.
        Key insight: ubuntu22-dev:layer-apt-gnupg-HASH contains ALL packages up to gnupg!
        """
        packages = []
        
        # Parse image tag
        if ":layer-apt-" in image_tag:
            parts = image_tag.split(":layer-apt-")[1].split("-")
            if len(parts) < 2:
                return packages
                
            # Get the package name (everything except last hash)
            package_name = "-".join(parts[:-1])
            # De-escape special characters
            actual_package = package_name.replace("_", "-").replace("plus", "+")
            
            # This image contains all APT packages up to and including this one
            # Use the layer cache to find the sequence
            found_target = False
            for layer_key in sorted(self.cache.get("layers", {}).keys()):
                layer_data = self.cache["layers"][layer_key]
                if layer_data.get("type") == "apt":
                    content = layer_data.get("content", "")
                    packages.append(f"apt:{content}")
                    
                    # Stop when we reach this package
                    if content == actual_package:
                        found_target = True
                        break
            
            # If not found in cache, at minimum we know this package is there
            if not found_target and actual_package:
                # Try to guess based on common package order
                common_packages = [
                    "openssh-server", "sudo", "curl", "wget", "git", "vim", "nano",
                    "htop", "build-essential", "cmake", "pkg-config", "libssl-dev",
                    "ca-certificates", "gnupg", "lsb-release", "net-tools",
                    "g++", "gdb", "valgrind", "clang", "libboost-all-dev", "docker.io"
                ]
                
                for pkg in common_packages:
                    packages.append(f"apt:{pkg}")
                    if pkg == actual_package:
                        break
        
        return packages
    
    def _reconstruct_all_packages(self) -> List[str]:
        """Reconstruct the full package list from all cached layers"""
        packages = []
        
        # Collect all apt/yum packages from layers in order
        for layer_key in sorted(self.cache.get("layers", {}).keys()):
            layer_data = self.cache["layers"][layer_key]
            if layer_data.get("type") in ["apt", "yum"]:
                packages.append(f"{layer_data['type']}:{layer_data['content']}")
        
        return packages
    
    def _get_cached_items(self, image_tag: str) -> List[str]:
        """Get cached items for an image"""
        image_sets = self._build_image_sets_from_layers()
        if image_tag in image_sets:
            return image_sets[image_tag].get("packages", [])
        return []
    
    def _discover_existing_images(self):
        """Discover Docker images that exist locally and add to cache"""
        try:
            # Get all ubuntu22-dev images
            cmd = ['docker', 'images', '--format', '{{.Repository}}:{{.Tag}}', 'ubuntu22-dev']
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            
            if result.returncode == 0:
                images = result.stdout.strip().split('\n')
                for image in images:
                    if image and image != "ubuntu22-dev:<none>":
                        # Check if this image is already in cache
                        if image not in self.cache["image_sets"]:
                            # Try to extract info from the image name
                            # Format: ubuntu22-dev:layer-TYPE-NAME-HASH
                            if "layer-" in image:
                                # This is a layer image, try to extract what it contains
                                # For now, we'll mark it as discovered
                                logging.debug(f"  🔍 Discovered uncached image: {image}")
                                
                                # Try to build package list from layer cache
                                packages = self._reconstruct_packages_for_image(image)
                                if packages:
                                    self.cache["image_sets"][image] = {
                                        "packages": packages,
                                        "discovered": True,
                                        "created": datetime.now().isoformat()
                                    }
                            elif image == "ubuntu22-dev:latest":
                                # This is the final image, try to reconstruct its contents
                                packages = self._reconstruct_packages_from_layers()
                                if packages:
                                    self.cache["image_sets"][image] = {
                                        "packages": packages,
                                        "discovered": True,
                                        "created": datetime.now().isoformat()
                                    }
        except Exception as e:
            logging.warning(f"Failed to discover images: {e}")
    
    def _reconstruct_packages_for_image(self, image_tag: str) -> List[str]:
        """Try to reconstruct package list for an image"""
        packages = []
        
        # Extract the layer name from image tag
        # Format: ubuntu22-dev:layer-apt-curl-HASH
        if ":layer-" in image_tag:
            parts = image_tag.split(":layer-")[1].split("-")
            if len(parts) >= 3:
                layer_type = parts[0]  # apt, yum, etc
                layer_name = "-".join(parts[1:-1])  # package name (may have dashes)
                
                # Look for this layer in cache
                for layer_key, layer_data in self.cache.get("layers", {}).items():
                    if layer_data.get("image") == image_tag:
                        # Found it! Build the cumulative package list up to this point
                        return self._get_cumulative_packages_up_to(layer_data)
        
        return packages
    
    def _get_cumulative_packages_up_to(self, target_layer: Dict) -> List[str]:
        """Get all packages up to and including a specific layer"""
        packages = []
        
        # Walk through all layers in order and collect packages
        for layer_key, layer_data in self.cache.get("layers", {}).items():
            if layer_data.get("type") in ["apt", "yum"]:
                packages.append(f"{layer_data['type']}:{layer_data['content']}")
            
            # Stop if we reached the target
            if layer_data.get("image") == target_layer.get("image"):
                break
        
        return packages
    
    def _reconstruct_packages_from_layers(self) -> List[str]:
        """Reconstruct the full package list from all cached layers"""
        packages = []
        
        # Collect all apt/yum packages from layers
        for layer_key, layer_data in self.cache.get("layers", {}).items():
            if layer_data.get("type") in ["apt", "yum"]:
                packages.append(f"{layer_data['type']}:{layer_data['content']}")
        
        return packages
    
    def _get_layer_key(self, layer: Layer) -> str:
        """Generate a unique key for a layer"""
        return f"{layer.type.value}-{layer.name}-{layer.hash}"
    
    def _image_exists(self, image_tag: str) -> bool:
        """Check if a Docker image exists locally"""
        if not image_tag:
            return False
        try:
            # Import sudo_prefix from build_orchestrator (use absolute import)
            import os
            if os.geteuid() != 0:
                cmd = ['sudo', '-E', 'docker', 'images', '-q', image_tag]
            else:
                cmd = ['docker', 'images', '-q', image_tag]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            exists = bool(result.stdout.strip())
            logging.info(f"Image check: {image_tag} -> {'exists' if exists else 'not found'}")
            if not exists:
                logging.info(f"  Command: {' '.join(cmd)}")
                logging.info(f"  Return code: {result.returncode}")
                logging.info(f"  Stderr: {result.stderr.strip()}")
            return exists
        except Exception as e:
            logging.warning(f"Error checking image {image_tag}: {e}")
            return False
    
    def get_cache_stats(self) -> Dict:
        """Get statistics about the cache"""
        return {
            "total_layers": len(self.cache.get("layers", {})),
            "total_images": len(self.cache.get("image_sets", {})),
            "cache_file": self.cache_file,
            "last_updated": self.cache.get("metadata", {}).get("last_updated", "unknown")
        }

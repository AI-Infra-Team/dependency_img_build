#!/usr/bin/env python3
"""
Simple and effective layer reuse manager.
Just find the best existing image and use it!
"""

import subprocess
import json
import os
from typing import List, Dict, Tuple, Set

class SimpleReuseManager:
    def __init__(self):
        self.cache_file = "simple_cache.json"
        
    def find_best_base(self, target_packages: List[str]) -> Tuple[str, List[str]]:
        """
        Find the best existing Docker image to use as base.
        Returns: (base_image, packages_to_build)
        """
        print("\n🔍 Finding best existing image...")
        
        # Get ALL docker images
        cmd = ['docker', 'images', '--format', '{{.Repository}}:{{.Tag}}', 'ubuntu22-dev']
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            return "ubuntu:22.04", target_packages
            
        images = [img for img in result.stdout.strip().split('\n') if img and ':layer-apt-' in img]
        
        if not images:
            print("❌ No existing images found")
            return "ubuntu:22.04", target_packages
        
        print(f"📦 Found {len(images)} existing images")
        
        # Parse each image to understand what package it represents
        image_packages = {}
        for image in images:
            # Extract package name from image tag
            # Format: ubuntu22-dev:layer-apt-PACKAGE-HASH
            if ':layer-apt-' in image:
                parts = image.split(':layer-apt-')[1].split('-')
                if len(parts) >= 2:
                    # Everything except last part (hash) is the package name
                    package = '-'.join(parts[:-1])
                    # De-escape
                    package = package.replace('_', '-').replace('plus', '+')
                    image_packages[image] = package
                    
        # Find the image with the package that appears latest in our target list
        best_image = None
        best_index = -1
        
        target_set = set(target_packages)
        
        for image, package in image_packages.items():
            if package in target_set:
                try:
                    idx = target_packages.index(package)
                    if idx > best_index:
                        best_index = idx
                        best_image = image
                except ValueError:
                    continue
        
        if best_image and best_index >= 0:
            # We found an image that has packages up to best_index
            packages_to_build = target_packages[best_index + 1:]
            print(f"✅ Found base: {best_image}")
            print(f"   Has {best_index + 1} packages, need to build {len(packages_to_build)} more")
            return best_image, packages_to_build
        else:
            print("❌ No suitable base found")
            return "ubuntu:22.04", target_packages
    
    def check_if_complete(self, target_packages: List[str]) -> str:
        """
        Check if we already have an image with ALL the packages.
        """
        # Check if ubuntu22-dev:latest exists and has all we need
        cmd = ['docker', 'images', '-q', 'ubuntu22-dev:latest']
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.stdout.strip():
            # Check our cache to see what's in it
            if os.path.exists(self.cache_file):
                with open(self.cache_file, 'r') as f:
                    cache = json.load(f)
                    latest_packages = cache.get('latest_packages', [])
                    
                    if set(target_packages).issubset(set(latest_packages)):
                        print("🎉 ubuntu22-dev:latest already has everything!")
                        return 'ubuntu22-dev:latest'
        
        # Also check for the last package in sequence
        if target_packages:
            last_package = target_packages[-1]
            escaped = last_package.replace('-', '_').replace('+', 'plus')
            
            # Look for an image with this package
            cmd = ['docker', 'images', '--format', '{{.Repository}}:{{.Tag}}', f'ubuntu22-dev']
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            for image in result.stdout.strip().split('\n'):
                if f':layer-apt-{escaped}-' in image:
                    print(f"🎉 Found complete image: {image}")
                    return image
        
        return None
    
    def save_build_info(self, final_image: str, packages: List[str]):
        """Save what packages are in the final image."""
        cache = {}
        if os.path.exists(self.cache_file):
            with open(self.cache_file, 'r') as f:
                cache = json.load(f)
        
        cache['latest_packages'] = packages
        cache['latest_image'] = final_image
        
        with open(self.cache_file, 'w') as f:
            json.dump(cache, f, indent=2)

if __name__ == "__main__":
    # Test with sample packages
    manager = SimpleReuseManager()
    
    packages = [
        "openssh-server", "sudo", "curl", "wget", "git", "vim", "nano",
        "htop", "build-essential", "cmake", "pkg-config", "libssl-dev",
        "ca-certificates", "gnupg", "lsb-release", "net-tools",
        "g++", "gdb", "valgrind", "clang", "libboost-all-dev", "docker.io"
    ]
    
    # First check if we already have everything
    complete = manager.check_if_complete(packages)
    if complete:
        print(f"Using existing image: {complete}")
    else:
        base, to_build = manager.find_best_base(packages)
        print(f"\nBase: {base}")
        print(f"Build: {to_build}")
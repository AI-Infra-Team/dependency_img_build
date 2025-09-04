#!/usr/bin/env python3
"""
test.py - APT install and remove test (Python version)
"""

import subprocess
import sys
import os
from pathlib import Path

# Colors for output
class Colors:
    RED = '\033[0;31m'
    GREEN = '\033[0;32m'
    YELLOW = '\033[1;33m'
    BLUE = '\033[0;34m'
    NC = '\033[0m'

def log_info(msg):
    print(f"{Colors.BLUE}[TEST-INFO]{Colors.NC} {msg}")

def log_success(msg):
    print(f"{Colors.GREEN}[TEST-SUCCESS]{Colors.NC} {msg}")

def log_warning(msg):
    print(f"{Colors.YELLOW}[TEST-WARNING]{Colors.NC} {msg}")

def log_error(msg):
    print(f"{Colors.RED}[TEST-ERROR]{Colors.NC} {msg}")

def run_command(cmd, capture_output=True, check=True):
    """Run a command and return the result"""
    try:
        result = subprocess.run(
            cmd, 
            shell=True, 
            capture_output=capture_output, 
            text=True, 
            check=check
        )
        return result
    except subprocess.CalledProcessError as e:
        if not capture_output:
            log_error(f"Command failed: {cmd}")
            log_error(f"Return code: {e.returncode}")
        return e

def docker_cmd(cmd: str) -> str:
    """Prefix docker command with sudo when not running as root"""
    try:
        if os.geteuid() != 0:
            return f"sudo -E {cmd}"
    except Exception:
        pass
    return cmd

def check_package_in_container(container_name, package_name):
    """Check if package is installed in container"""
    cmd = docker_cmd(f'docker exec {container_name} dpkg -l {package_name} 2>/dev/null')
    result = run_command(cmd, check=False)
    if isinstance(result, subprocess.CalledProcessError):
        return False
    return 'ii' in result.stdout

def verify_packages(container_name, packages):
    """Verify that packages are installed in container"""
    log_info(f"Verifying packages in container: {container_name}")
    
    for pkg in packages:
        if check_package_in_container(container_name, pkg):
            log_success(f"✅ Package {pkg} is installed")
        else:
            log_error(f"❌ Package {pkg} is NOT installed")
            return False
    return True

def verify_packages_not_installed(container_name, packages):
    """Verify that packages are NOT installed in container"""
    log_info(f"Verifying packages are NOT installed in container: {container_name}")
    
    for pkg in packages:
        if check_package_in_container(container_name, pkg):
            log_error(f"❌ Package {pkg} should NOT be installed but was found")
            return False
        else:
            log_success(f"✅ Package {pkg} is correctly NOT installed")
    return True

def cleanup():
    """Cleanup containers and images"""
    log_info("Cleaning up containers and images...")
    
    # Stop and remove containers
    run_command(docker_cmd("docker stop test-apt-demo-container 2>/dev/null || true"), check=False)
    run_command(docker_cmd("docker rm test-apt-demo-container 2>/dev/null || true"), check=False)
    run_command(docker_cmd("docker stop test-apt-demo-reduced-container 2>/dev/null || true"), check=False)
    run_command(docker_cmd("docker rm test-apt-demo-reduced-container 2>/dev/null || true"), check=False)
    
    # Remove images
    run_command(docker_cmd("docker rmi test-apt-demo:latest 2>/dev/null || true"), check=False)
    run_command(docker_cmd("docker rmi test-apt-demo-reduced:latest 2>/dev/null || true"), check=False)
    
    log_info("Cleanup completed")

def main():
    """Main test execution"""
    log_info("Starting APT install/remove test")
    
    # Change to project root
    project_root = Path(__file__).parent.parent.parent
    os.chdir(project_root)
    
    # Cleanup any existing containers/images
    cleanup()
    
    try:
        # Phase 1: Build image with 3 packages (curl, wget, htop)
        log_info("=== Phase 1: Building image with curl, wget, htop ===")
        
        result = run_command("python3 cli.py build -c tests/apt_install_remove_test/config.yaml", capture_output=False)
        if isinstance(result, subprocess.CalledProcessError):
            log_error("Failed to build initial image with 3 packages")
            cleanup()
            return False
        
        # Start container for first phase
        log_info("Starting container for package verification...")
        run_command(docker_cmd("docker run -d --name test-apt-demo-container test-apt-demo:latest tail -f /dev/null"))
        
        # Verify all 3 packages are installed
        if not verify_packages("test-apt-demo-container", ["curl", "wget", "htop"]):
            log_error("Phase 1 failed: Not all packages are installed")
            cleanup()
            return False
        
        log_success("Phase 1 completed: All packages successfully installed")
        
        # Stop first container
        run_command(docker_cmd("docker stop test-apt-demo-container"))
        run_command(docker_cmd("docker rm test-apt-demo-container"))
        
        # Phase 2: Build image with 2 packages (curl, wget) - htop should be removed
        log_info("=== Phase 2: Building image with curl, wget (htop removed) ===")
        
        result = run_command("python3 cli.py build -c tests/apt_install_remove_test/config_reduced.yaml", capture_output=False)
        if isinstance(result, subprocess.CalledProcessError):
            log_error("Failed to build reduced image with 2 packages")
            cleanup()
            return False
        
        # Start container for second phase
        log_info("Starting container for removal verification...")
        run_command(docker_cmd("docker run -d --name test-apt-demo-reduced-container test-apt-demo-reduced:latest tail -f /dev/null"))
        
        # Verify curl and wget are still installed
        if not verify_packages("test-apt-demo-reduced-container", ["curl", "wget"]):
            log_error("Phase 2 failed: Required packages are missing")
            cleanup()
            return False
        
        # Verify htop is NOT installed (removed)
        if not verify_packages_not_installed("test-apt-demo-reduced-container", ["htop"]):
            log_error("Phase 2 failed: htop should have been removed but is still installed")
            cleanup()
            return False
        
        log_success("Phase 2 completed: htop successfully removed, curl and wget retained")
        
        # Cleanup
        cleanup()
        
        log_success("🎉 APT install/remove test completed successfully!")
        log_success("   ✅ Initial installation of 3 packages works")
        log_success("   ✅ Package removal works correctly")
        log_success("   ✅ Remaining packages are preserved")
        
        return True
        
    except Exception as e:
        log_error(f"Test failed with exception: {str(e)}")
        cleanup()
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)

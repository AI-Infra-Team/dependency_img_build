#!/bin/bash
# apt_install.sh - Smart APT package installer for Docker builds
# Usage: apt_install.sh package1 package2 package3...

set -e

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if running as root or with sudo
check_permissions() {
    if [[ $EUID -ne 0 ]]; then
        log_error "This script must be run as root or with sudo"
        exit 1
    fi
}

# Update package list if it's old or doesn't exist
update_package_list() {
    local apt_updated=false
    
    # Check if we need to update
    if [[ ! -f /var/lib/apt/lists/lock ]] || [[ $(find /var/lib/apt/lists -type f -name '*Packages*' -mtime +1 | wc -l) -gt 0 ]]; then
        log_info "Updating package lists..."
        apt-get update -qq
        apt_updated=true
    else
        log_info "Package lists are up to date"
    fi
    
    echo "$apt_updated"
}

# Check if package is already installed
is_package_installed() {
    local package="$1"
    dpkg -l "$package" 2>/dev/null | grep -q "^ii"
}

# Install packages efficiently
install_packages() {
    local packages=("$@")
    local to_install=()
    local already_installed=()
    
    log_info "Checking package status..."
    
    # Check which packages need installation
    for package in "${packages[@]}"; do
        if is_package_installed "$package"; then
            already_installed+=("$package")
        else
            to_install+=("$package")
        fi
    done
    
    # Report already installed packages
    if [[ ${#already_installed[@]} -gt 0 ]]; then
        log_info "Already installed: ${already_installed[*]}"
    fi
    
    # Install needed packages
    if [[ ${#to_install[@]} -gt 0 ]]; then
        log_info "Installing: ${to_install[*]}"
        
        # Use DEBIAN_FRONTEND=noninteractive to avoid prompts
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${to_install[@]}"
        
        log_success "Successfully installed: ${to_install[*]}"
    else
        log_info "No packages need installation"
    fi
}

# Clean up after installation
cleanup() {
    log_info "Cleaning up..."
    apt-get clean
    rm -rf /var/lib/apt/lists/*
    log_success "Cleanup completed"
}

# Main execution
main() {
    log_info "APT Install Tool - Starting installation process"
    log_info "Proxy settings: http_proxy=$http_proxy https_proxy=$https_proxy"
    
    # Validate input
    if [[ $# -eq 0 ]]; then
        log_error "Usage: $0 package1 [package2 ...]"
        exit 1
    fi
    
    # Check permissions
    check_permissions
    
    # Update package lists if needed
    apt_updated=$(update_package_list)
    
    # Install packages
    install_packages "$@"
    
    # Clean up only if we updated apt lists
    if [[ "$apt_updated" == "true" ]]; then
        cleanup
    else
        log_info "Skipping cleanup (apt lists not updated)"
    fi
    
    log_success "Installation completed successfully"
}

# Execute main function with all arguments
main "$@"
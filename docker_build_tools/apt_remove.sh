#!/bin/bash
# apt_remove.sh - Smart APT package remover for Docker builds
# Usage: apt_remove.sh package1 package2 package3...

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

# Check if package is installed
is_package_installed() {
    local package="$1"
    dpkg -l "$package" 2>/dev/null | grep -q "^ii"
}

# Check if package is safe to remove (no critical dependencies)
is_safe_to_remove() {
    local package="$1"
    
    # Get list of packages that depend on this package
    local dependents=$(apt-cache rdepends --no-recommends --no-suggests --no-conflicts --no-breaks --no-replaces --no-enhances "$package" 2>/dev/null | grep -v "Reverse Depends:" | grep -v "^$package$" | wc -l)
    
    if [[ $dependents -eq 0 ]]; then
        return 0  # Safe to remove
    else
        return 1  # Has dependencies
    fi
}

# Remove packages safely
remove_packages() {
    local packages=("$@")
    local to_remove=()
    local not_installed=()
    local unsafe_to_remove=()
    
    log_info "Checking package removal safety..."
    
    # Check each package
    for package in "${packages[@]}"; do
        if ! is_package_installed "$package"; then
            not_installed+=("$package")
        elif is_safe_to_remove "$package"; then
            to_remove+=("$package")
        else
            unsafe_to_remove+=("$package")
        fi
    done
    
    # Report status
    if [[ ${#not_installed[@]} -gt 0 ]]; then
        log_info "Not installed: ${not_installed[*]}"
    fi
    
    if [[ ${#unsafe_to_remove[@]} -gt 0 ]]; then
        log_warning "Unsafe to remove (has dependencies): ${unsafe_to_remove[*]}"
    fi
    
    # Remove safe packages
    if [[ ${#to_remove[@]} -gt 0 ]]; then
        log_info "Removing: ${to_remove[*]}"
        
        # Remove packages
        apt-get remove -y "${to_remove[@]}"
        
        log_success "Successfully removed: ${to_remove[*]}"
        
        # Auto-remove orphaned packages
        log_info "Removing orphaned dependencies..."
        apt-get autoremove -y
        
    else
        log_info "No packages can be safely removed"
    fi
}

# Clean up after removal
cleanup() {
    log_info "Cleaning up..."
    apt-get autoclean
    log_success "Cleanup completed"
}

# Main execution
main() {
    log_info "APT Remove Tool - Starting removal process"
    
    # Validate input
    if [[ $# -eq 0 ]]; then
        log_error "Usage: $0 package1 [package2 ...]"
        exit 1
    fi
    
    # Check permissions
    check_permissions
    
    # Remove packages
    remove_packages "$@"
    
    # Clean up
    cleanup
    
    log_success "Removal process completed"
}

# Execute main function with all arguments
main "$@"
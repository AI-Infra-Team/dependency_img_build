#!/usr/bin/env python3
"""
Test runner for dependency_img_build
"""

import os
import sys
import subprocess
import argparse
from pathlib import Path
from typing import List, Tuple

# Colors for output
class Colors:
    RED = '\033[0;31m'
    GREEN = '\033[0;32m'
    YELLOW = '\033[1;33m'
    BLUE = '\033[0;34m'
    NC = '\033[0m'

def log_info(message: str):
    print(f"{Colors.BLUE}[INFO]{Colors.NC} {message}")

def log_success(message: str):
    print(f"{Colors.GREEN}[SUCCESS]{Colors.NC} {message}")

def log_warning(message: str):
    print(f"{Colors.YELLOW}[WARNING]{Colors.NC} {message}")

def log_error(message: str):
    print(f"{Colors.RED}[ERROR]{Colors.NC} {message}")

def run_test_case(test_dir: Path) -> bool:
    """Run a single test case"""
    test_name = test_dir.name
    
    log_info("=" * 48)
    log_info(f"Running test case: {test_name}")
    log_info("=" * 48)
    
    # Check if test directory has required files
    config_file = test_dir / "config.yaml"
    test_script = test_dir / "test.sh"
    
    if not config_file.exists():
        log_error(f"Missing config.yaml in {test_dir}")
        return False
    
    if not test_script.exists():
        log_error(f"Missing test.sh in {test_dir}")
        return False
    
    # Make test script executable
    os.chmod(test_script, 0o755)
    
    # Change to test directory and run the test
    original_cwd = os.getcwd()
    try:
        os.chdir(test_dir)
        result = subprocess.run(['./test.sh'], capture_output=False)
        
        if result.returncode == 0:
            log_success(f"Test case {test_name} PASSED")
            return True
        else:
            log_error(f"Test case {test_name} FAILED")
            return False
    except Exception as e:
        log_error(f"Error running test {test_name}: {e}")
        return False
    finally:
        os.chdir(original_cwd)

def find_test_directories(tests_dir: Path) -> List[Path]:
    """Find all test directories"""
    test_dirs = []
    if tests_dir.exists():
        for item in tests_dir.iterdir():
            if item.is_dir() and not item.name.startswith('.'):
                test_dirs.append(item)
    return sorted(test_dirs)

def main():
    parser = argparse.ArgumentParser(description='Test runner for dependency_img_build')
    parser.add_argument('--test', '-t', help='Run specific test case')
    parser.add_argument('--list', '-l', action='store_true', help='List available tests')
    args = parser.parse_args()
    
    # Get project root and tests directory
    project_root = Path(__file__).parent.parent.absolute()
    tests_dir = project_root / "tests"
    
    # Change to project root
    os.chdir(project_root)
    
    log_info(f"Starting test suite for dependency_img_build")
    log_info(f"Project root: {project_root}")
    log_info(f"Tests directory: {tests_dir}")
    
    # Find all test directories
    test_dirs = find_test_directories(tests_dir)
    
    if args.list:
        log_info("Available test cases:")
        for test_dir in test_dirs:
            print(f"  - {test_dir.name}")
        return 0
    
    if args.test:
        # Run specific test
        specific_test_dir = tests_dir / args.test
        if not specific_test_dir.exists():
            log_error(f"Test case '{args.test}' not found")
            return 1
        
        if run_test_case(specific_test_dir):
            log_success("Test passed!")
            return 0
        else:
            log_error("Test failed!")
            return 1
    
    # Run all tests
    failed_tests = 0
    total_tests = len(test_dirs)
    
    if total_tests == 0:
        log_warning("No test cases found")
        return 0
    
    for test_dir in test_dirs:
        if not run_test_case(test_dir):
            failed_tests += 1
        print()
    
    # Summary
    log_info("=" * 48)
    log_info("Test Summary")
    log_info("=" * 48)
    log_info(f"Total tests: {total_tests}")
    log_info(f"Passed: {total_tests - failed_tests}")
    
    if failed_tests == 0:
        log_success("All tests passed!")
        return 0
    else:
        log_error(f"Failed: {failed_tests}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
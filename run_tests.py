#!/usr/bin/env python3
"""
run_tests.py - Python test runner for dependency_img_build
"""

import os
import sys
import importlib.util
from pathlib import Path

# Colors for output
class Colors:
    RED = '\033[0;31m'
    GREEN = '\033[0;32m'
    YELLOW = '\033[1;33m'
    BLUE = '\033[0;34m'
    NC = '\033[0m'

def log_info(msg):
    print(f"{Colors.BLUE}[INFO]{Colors.NC} {msg}")

def log_success(msg):
    print(f"{Colors.GREEN}[SUCCESS]{Colors.NC} {msg}")

def log_warning(msg):
    print(f"{Colors.YELLOW}[WARNING]{Colors.NC} {msg}")

def log_error(msg):
    print(f"{Colors.RED}[ERROR]{Colors.NC} {msg}")

def run_test_case(test_dir):
    """Run a single test case"""
    test_name = test_dir.name
    
    log_info("=" * 40)
    log_info(f"Running test case: {test_name}")
    log_info("=" * 40)
    
    # Check if test directory has required files
    config_file = test_dir / "config.yaml"
    if not config_file.exists():
        log_error(f"Missing config.yaml in {test_dir}")
        return False
    
    test_script = test_dir / "test.py"
    if not test_script.exists():
        log_error(f"Missing test.py in {test_dir}")
        return False
    
    # Load and run the test module
    try:
        # Make test script executable
        os.chmod(test_script, 0o755)
        
        # Load the test module
        spec = importlib.util.spec_from_file_location("test_module", test_script)
        test_module = importlib.util.module_from_spec(spec)
        
        # Add the test directory to sys.path temporarily
        original_path = sys.path.copy()
        sys.path.insert(0, str(test_dir))
        
        try:
            spec.loader.exec_module(test_module)
            
            # Run the test's main function
            if hasattr(test_module, 'main'):
                result = test_module.main()
                if result:
                    log_success(f"Test case {test_name} PASSED")
                    return True
                else:
                    log_error(f"Test case {test_name} FAILED")
                    return False
            else:
                log_error(f"Test module {test_name} has no main() function")
                return False
                
        finally:
            # Restore original sys.path
            sys.path = original_path
            
    except Exception as e:
        log_error(f"Failed to run test case {test_name}: {str(e)}")
        return False

def main():
    """Main execution"""
    project_root = Path(__file__).parent
    tests_dir = project_root / "tests"
    
    failed_tests = 0
    total_tests = 0
    
    log_info("Starting test suite for dependency_img_build")
    
    # Change to project root
    os.chdir(project_root)
    
    # Find all test directories
    if not tests_dir.exists():
        log_error(f"Tests directory not found: {tests_dir}")
        return False
    
    test_dirs = [d for d in tests_dir.iterdir() if d.is_dir()]
    
    if not test_dirs:
        log_warning("No test directories found")
        return True
    
    for test_dir in test_dirs:
        total_tests += 1
        if not run_test_case(test_dir):
            failed_tests += 1
        print()  # Empty line between tests
    
    # Summary
    log_info("=" * 40)
    log_info("Test Summary")
    log_info("=" * 40)
    log_info(f"Total tests: {total_tests}")
    log_info(f"Passed: {total_tests - failed_tests}")
    
    if failed_tests == 0:
        log_success("All tests passed!")
        return True
    else:
        log_error(f"Failed: {failed_tests}")
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
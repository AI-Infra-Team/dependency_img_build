#!/usr/bin/env python3

import argparse
import json
import sys
import os
from typing import Dict, Any

from config import CacheConfig
from build_orchestrator import BuildOrchestrator


def load_cache_config(config_path: str = None) -> CacheConfig:
    """Load cache configuration from file or environment"""
    config = CacheConfig()
    
    # Load from config file if provided
    if config_path and os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                config_data = json.load(f)
            
            for key, value in config_data.items():
                if hasattr(config, key):
                    setattr(config, key, value)
        except Exception as e:
            print(f"Warning: Failed to load cache config: {e}")
    
    # Override with environment variables
    env_mapping = {
        'CACHE_LOCAL_PATH': 'local_path',
        'MINIO_ENDPOINT': 'minio_endpoint',
        'MINIO_BUCKET': 'minio_bucket',
        'MINIO_ACCESS_KEY': 'minio_access_key',
        'MINIO_SECRET_KEY': 'minio_secret_key',
        'GHCR_REGISTRY': 'ghcr_registry',
        'GHCR_NAMESPACE': 'ghcr_namespace',
        'GHCR_TOKEN': 'ghcr_token',
    }
    
    for env_var, config_attr in env_mapping.items():
        if env_var in os.environ:
            setattr(config, config_attr, os.environ[env_var])
    
    return config


def cmd_build(args):
    """Build command handler"""
    cache_config = load_cache_config(args.cache_config)
    orchestrator = BuildOrchestrator(cache_config)
    
    if not os.path.exists(args.config):
        print(f"Error: Configuration file '{args.config}' not found")
        return 1
    
    success = orchestrator.build_image(
        config_file=args.config,
        force_rebuild=args.force_rebuild
    )
    
    return 0 if success else 1


def cmd_status(args):
    """Status command handler"""
    cache_config = load_cache_config(args.cache_config)
    orchestrator = BuildOrchestrator(cache_config)
    
    status = orchestrator.show_build_status(args.config)
    
    print("=== Build Status ===")
    print(f"Cache Statistics:")
    print(f"  Total cached steps: {status['cache_stats']['total_cached_steps']}")
    print(f"  Recent builds: {status['cache_stats']['recent_builds']}")
    
    print(f"\nCache Levels Available:")
    print(f"  Local: {'✓' if status['cache_levels']['local'] else '✗'}")
    print(f"  MinIO: {'✓' if status['cache_levels']['minio'] else '✗'}")  
    print(f"  GHCR: {'✓' if status['cache_levels']['ghcr'] else '✗'}")
    
    # Display stage change frequency
    if 'stage_change_frequency' in status:
        print(f"\nStage Change Frequency (last 10 builds):")
        for stage_name, frequency in status['stage_change_frequency'].items():
            indicator = "🔥" if frequency > 0.5 else "📊" if frequency > 0.2 else "✅"
            print(f"  {indicator} {stage_name}: {frequency:.1%}")
    
    # Display stage changes in current build
    if 'stage_changes' in status:
        changed = [name for name, changed in status['stage_changes'].items() if changed]
        if changed:
            print(f"\nChanged Stages in Current Build:")
            for stage in changed:
                print(f"  🔄 {stage}")
        else:
            print(f"\nNo stage changes detected")
    
    # Display stage ordering information
    if 'stage_order' in status:
        order_info = status['stage_order']
        print(f"\nStage Execution Order:")
        print(f"  Original: {' -> '.join(order_info['original'])}")
        if order_info['original'] != order_info['optimized']:
            print(f"  Optimized: {' -> '.join(order_info['optimized'])}")
            print(f"  Reordered stages: {order_info['reordered_count']}")
    
    if 'current_config' in status:
        if 'error' in status['current_config']:
            print(f"\nCurrent Config: Error - {status['current_config']['error']}")
        else:
            print(f"\nCurrent Configuration Analysis:")
            print(f"  Total steps: {status['current_config']['total_steps']}")
            print(f"  Cached steps: {status['current_config']['cached_steps']}")
            print(f"  Rebuild steps: {status['current_config']['rebuild_steps']}")
            print(f"  Cache efficiency: {status['current_config']['efficiency']}")
    
    return 0


def cmd_clean(args):
    """Clean command handler"""
    cache_config = load_cache_config(args.cache_config)
    orchestrator = BuildOrchestrator(cache_config)
    
    success = orchestrator.clean_cache(max_age_days=args.max_age)
    return 0 if success else 1


def cmd_init(args):
    """Initialize command handler - create example config"""
    example_config = {
        "user": "app",
        "sudo": True,
        "apt_packages": [
            "curl",
            "git",
            "python3",
            "python3-pip"
        ],
        "env_scripts": [
            "echo 'Setting up environment'",
            "pip3 install --upgrade pip"
        ],
        "stages": [
            {
                "name": "dependencies",
                "dependencies": [],
                "commands": [
                    "pip3 install requests",
                    "pip3 install flask"
                ]
            },
            {
                "name": "application",
                "dependencies": ["dependencies"],
                "commands": [
                    "mkdir -p /app",
                    "echo 'FROM dependencies stage' > /app/README.md"
                ]
            }
        ]
    }
    
    config_file = args.output or "build-config.json"
    
    try:
        with open(config_file, 'w') as f:
            json.dump(example_config, f, indent=2)
        print(f"Example configuration created: {config_file}")
        return 0
    except Exception as e:
        print(f"Error creating config file: {e}")
        return 1


def main():
    parser = argparse.ArgumentParser(
        description="Dynamic Docker Build System with Multi-level Caching",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s build -c config.json
  %(prog)s status -c config.json  
  %(prog)s clean --max-age 7
  %(prog)s init --output my-config.json
        """
    )
    
    parser.add_argument(
        '--cache-config', 
        help='Path to cache configuration file'
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Build command
    build_parser = subparsers.add_parser('build', help='Build Docker image')
    build_parser.add_argument(
        '-c', '--config', 
        required=True,
        help='Path to build configuration file (JSON/YAML)'
    )
    build_parser.add_argument(
        '--force-rebuild',
        action='store_true',
        help='Force rebuild all steps, ignore cache'
    )
    build_parser.set_defaults(func=cmd_build)
    
    # Status command
    status_parser = subparsers.add_parser('status', help='Show build status')
    status_parser.add_argument(
        '-c', '--config',
        help='Path to build configuration file (optional)'
    )
    status_parser.set_defaults(func=cmd_status)
    
    # Clean command
    clean_parser = subparsers.add_parser('clean', help='Clean old cache entries')
    clean_parser.add_argument(
        '--max-age',
        type=int,
        default=30,
        help='Maximum age in days for cache entries (default: 30)'
    )
    clean_parser.set_defaults(func=cmd_clean)
    
    # Init command
    init_parser = subparsers.add_parser('init', help='Create example configuration')
    init_parser.add_argument(
        '--output', '-o',
        help='Output file path (default: build-config.json)'
    )
    init_parser.set_defaults(func=cmd_init)
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 1
    
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        return 1


if __name__ == '__main__':
    sys.exit(main())
import os
import re
from typing import Dict, List, Optional, Set
from dataclasses import dataclass


@dataclass
class EnvVarConfig:
    """Environment variable configuration"""
    inherit_proxy: bool = True
    inherit_locale: bool = True
    inherit_timezone: bool = True
    inherit_custom: List[str] = None
    exclude_vars: List[str] = None
    
    def __post_init__(self):
        if self.inherit_custom is None:
            self.inherit_custom = []
        if self.exclude_vars is None:
            self.exclude_vars = []


class EnvironmentManager:
    """Manages system environment variable extraction and injection"""
    
    # Common proxy environment variables
    PROXY_VARS = {
        'http_proxy', 'https_proxy', 'ftp_proxy', 'socks_proxy',
        'HTTP_PROXY', 'HTTPS_PROXY', 'FTP_PROXY', 'SOCKS_PROXY',
        'no_proxy', 'NO_PROXY', 'all_proxy', 'ALL_PROXY'
    }
    
    # Locale and language variables
    LOCALE_VARS = {
        'LANG', 'LANGUAGE', 'LC_ALL', 'LC_CTYPE', 'LC_NUMERIC',
        'LC_TIME', 'LC_COLLATE', 'LC_MONETARY', 'LC_MESSAGES',
        'LC_PAPER', 'LC_NAME', 'LC_ADDRESS', 'LC_TELEPHONE',
        'LC_MEASUREMENT', 'LC_IDENTIFICATION'
    }
    
    # Timezone variables
    TIMEZONE_VARS = {'TZ', 'TIMEZONE'}
    
    # Variables that should typically be excluded from Docker builds
    EXCLUDE_VARS = {
        'PATH', 'HOME', 'USER', 'USERNAME', 'LOGNAME', 'SHELL',
        'PWD', 'OLDPWD', 'SHLVL', '_', 'PS1', 'PS2', 'PS4',
        'SSH_AUTH_SOCK', 'SSH_AGENT_PID', 'DISPLAY', 'XAUTHORITY',
        'TERM', 'TERMINFO', 'COLUMNS', 'LINES',
        'XDG_RUNTIME_DIR', 'XDG_SESSION_ID', 'XDG_SESSION_TYPE',
        'DBUS_SESSION_BUS_ADDRESS', 'DESKTOP_SESSION'
    }
    
    def __init__(self, config: EnvVarConfig = None):
        self.config = config or EnvVarConfig()
    
    def extract_system_env_vars(self) -> Dict[str, str]:
        """Extract relevant system environment variables"""
        extracted = {}
        
        # Get all environment variables
        all_env = dict(os.environ)
        
        # Apply extraction rules
        if self.config.inherit_proxy:
            extracted.update(self._filter_vars(all_env, self.PROXY_VARS))
        
        if self.config.inherit_locale:
            extracted.update(self._filter_vars(all_env, self.LOCALE_VARS))
        
        if self.config.inherit_timezone:
            extracted.update(self._filter_vars(all_env, self.TIMEZONE_VARS))
        
        # Add custom variables
        for var_pattern in self.config.inherit_custom:
            if '*' in var_pattern or '?' in var_pattern:
                # Pattern matching
                pattern = re.compile(var_pattern.replace('*', '.*').replace('?', '.'))
                for env_name, env_value in all_env.items():
                    if pattern.match(env_name):
                        extracted[env_name] = env_value
            else:
                # Exact match
                if var_pattern in all_env:
                    extracted[var_pattern] = all_env[var_pattern]
        
        # Remove excluded variables
        exclude_set = self.EXCLUDE_VARS.union(set(self.config.exclude_vars))
        extracted = {k: v for k, v in extracted.items() if k not in exclude_set}
        
        return extracted
    
    def _filter_vars(self, env_dict: Dict[str, str], var_set: Set[str]) -> Dict[str, str]:
        """Filter environment variables by a given set"""
        return {k: v for k, v in env_dict.items() if k in var_set}
    
    def generate_env_dockerfile_lines(self, env_vars: Dict[str, str]) -> List[str]:
        """Generate Dockerfile ENV lines for the given environment variables"""
        lines = []
        
        if not env_vars:
            return lines
        
        lines.append("# Inherited system environment variables")
        
        # Group related variables
        proxy_vars = {k: v for k, v in env_vars.items() if k.lower().endswith('_proxy') or k.lower() == 'no_proxy'}
        locale_vars = {k: v for k, v in env_vars.items() if k in self.LOCALE_VARS}
        timezone_vars = {k: v for k, v in env_vars.items() if k in self.TIMEZONE_VARS}
        other_vars = {k: v for k, v in env_vars.items() 
                     if k not in proxy_vars and k not in locale_vars and k not in timezone_vars}
        
        # Add proxy variables
        if proxy_vars:
            lines.append("# Proxy configuration")
            for key, value in sorted(proxy_vars.items()):
                # Escape special characters in environment variable values
                escaped_value = self._escape_env_value(value)
                lines.append(f'ENV {key}="{escaped_value}"')
            lines.append("")
        
        # Add locale variables
        if locale_vars:
            lines.append("# Locale configuration")
            for key, value in sorted(locale_vars.items()):
                escaped_value = self._escape_env_value(value)
                lines.append(f'ENV {key}="{escaped_value}"')
            lines.append("")
        
        # Add timezone variables
        if timezone_vars:
            lines.append("# Timezone configuration")
            for key, value in sorted(timezone_vars.items()):
                escaped_value = self._escape_env_value(value)
                lines.append(f'ENV {key}="{escaped_value}"')
            lines.append("")
        
        # Add other variables
        if other_vars:
            lines.append("# Other inherited variables")
            for key, value in sorted(other_vars.items()):
                escaped_value = self._escape_env_value(value)
                lines.append(f'ENV {key}="{escaped_value}"')
            lines.append("")
        
        return lines
    
    def _escape_env_value(self, value: str) -> str:
        """Escape special characters in environment variable values for Dockerfile"""
        # Escape backslashes and quotes
        value = value.replace('\\', '\\\\')
        value = value.replace('"', '\\"')
        value = value.replace('$', '\\$')
        return value
    
    def get_docker_build_args(self, env_vars: Dict[str, str]) -> List[str]:
        """Generate Docker build arguments for environment variables"""
        args = []
        for key, value in env_vars.items():
            args.extend(['--build-arg', f'{key}={value}'])
        return args
    
    def print_inherited_vars_summary(self, env_vars: Dict[str, str]):
        """Print a summary of inherited environment variables"""
        if not env_vars:
            print("🔒 No system environment variables inherited")
            return
        
        print(f"🌍 Inherited {len(env_vars)} system environment variables:")
        
        # Group by category for display
        proxy_vars = [k for k in env_vars.keys() if k.lower().endswith('_proxy') or k.lower() == 'no_proxy']
        locale_vars = [k for k in env_vars.keys() if k in self.LOCALE_VARS]
        timezone_vars = [k for k in env_vars.keys() if k in self.TIMEZONE_VARS]
        other_vars = [k for k in env_vars.keys() 
                     if k not in proxy_vars and k not in locale_vars and k not in timezone_vars]
        
        if proxy_vars:
            print(f"  🌐 Proxy: {', '.join(proxy_vars)}")
        if locale_vars:
            print(f"  🗣️  Locale: {', '.join(locale_vars)}")
        if timezone_vars:
            print(f"  🕐 Timezone: {', '.join(timezone_vars)}")
        if other_vars:
            print(f"  ⚙️  Other: {', '.join(other_vars)}")
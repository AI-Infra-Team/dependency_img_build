import json
import os
import hashlib
from typing import List, Dict, Optional, Set
from config import BuildStep, CacheLevel, Stage
from datetime import datetime


class BuildTracker:
    def __init__(self, cache_file: str = ".build_cache.json"):
        self.cache_file = cache_file
        self.build_history = self._load_history()
    
    def _load_history(self) -> Dict:
        """Load build history from cache file"""
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return {"builds": [], "step_cache": {}, "stage_history": {}, "stage_hashes": {}}
    
    def _save_history(self):
        """Save build history to cache file"""
        with open(self.cache_file, 'w') as f:
            json.dump(self.build_history, f, indent=2)
    
    def analyze_changes(self, new_steps: List[BuildStep]) -> List[str]:
        """
        Analyze which steps need to be rebuilt based on changes
        Returns list of actions: 'keep', 'rebuild'
        """
        if not self.build_history["builds"]:
            return ['rebuild'] * len(new_steps)
        
        last_build = self.build_history["builds"][-1]
        last_steps = last_build.get("steps", [])
        
        actions = []
        rebuild_from_index = None
        
        for i, new_step in enumerate(new_steps):
            new_hash = new_step.hash
            
            if i < len(last_steps) and last_steps[i]["hash"] == new_hash:
                if rebuild_from_index is None:
                    actions.append('keep')
                else:
                    actions.append('rebuild')
            else:
                if rebuild_from_index is None:
                    rebuild_from_index = i
                actions.append('rebuild')
        
        return actions
    
    def record_build(self, steps: List[BuildStep], image_tag: str):
        """Record a successful build"""
        build_record = {
            "timestamp": datetime.now().isoformat(),
            "image_tag": image_tag,
            "steps": [
                {
                    "stage_name": step.stage_name,
                    "command": step.command,
                    "hash": step.hash,
                    "cached": step.cached,
                    "cache_level": step.cache_level.value if step.cache_level else None
                }
                for step in steps
            ]
        }
        
        self.build_history["builds"].append(build_record)
        
        # Update step cache
        for step in steps:
            self.build_history["step_cache"][step.hash] = {
                "stage_name": step.stage_name,
                "command": step.command,
                "last_used": datetime.now().isoformat(),
                "cache_level": step.cache_level.value if step.cache_level else None
            }
        
        self._save_history()
    
    def get_cached_steps(self) -> Dict[str, Dict]:
        """Get all cached steps"""
        return self.build_history.get("step_cache", {})
    
    def is_step_cached(self, step_hash: str, cache_level: CacheLevel = None) -> bool:
        """Check if a step is cached at specified level or any level"""
        cached_steps = self.get_cached_steps()
        if step_hash not in cached_steps:
            return False
        
        if cache_level is None:
            return True
        
        step_cache_level = cached_steps[step_hash].get("cache_level")
        if step_cache_level is None:
            return False
        
        # Check cache level hierarchy: local < minio < ghcr
        level_hierarchy = {
            CacheLevel.LOCAL: 0,
            CacheLevel.MINIO: 1, 
            CacheLevel.GHCR: 2
        }
        
        return level_hierarchy.get(CacheLevel(step_cache_level), -1) >= level_hierarchy.get(cache_level, -1)
    
    def update_step_cache_level(self, step_hash: str, cache_level: CacheLevel):
        """Update the cache level for a step"""
        if step_hash in self.build_history["step_cache"]:
            self.build_history["step_cache"][step_hash]["cache_level"] = cache_level.value
            self._save_history()
    
    def cleanup_old_builds(self, keep_last: int = 10):
        """Clean up old build records"""
        builds = self.build_history["builds"]
        if len(builds) > keep_last:
            self.build_history["builds"] = builds[-keep_last:]
            self._save_history()
    
    def get_rebuild_plan(self, new_steps: List[BuildStep]) -> Dict:
        """Get detailed rebuild plan with statistics"""
        actions = self.analyze_changes(new_steps)
        
        keep_count = actions.count('keep')
        rebuild_count = actions.count('rebuild')
        
        plan = {
            "total_steps": len(new_steps),
            "keep_steps": keep_count,
            "rebuild_steps": rebuild_count,
            "efficiency": keep_count / len(new_steps) if new_steps else 0,
            "actions": actions,
            "first_changed_step": actions.index('rebuild') if 'rebuild' in actions else None
        }
        
        return plan
    
    def calculate_stage_hash(self, stage: Stage) -> str:
        """Calculate hash for a stage based on its commands and dependencies"""
        stage_data = {
            'name': stage.name,
            'dependencies': sorted(stage.dependencies),
            'commands': stage.commands
        }
        content = json.dumps(stage_data, sort_keys=True)
        return hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]
    
    def detect_stage_changes(self, stages: List[Stage]) -> Dict[str, bool]:
        """Detect which stages have changed since last build"""
        current_hashes = {}
        changed_stages = {}
        
        for stage in stages:
            stage_hash = self.calculate_stage_hash(stage)
            current_hashes[stage.name] = stage_hash
            
            last_hash = self.build_history.get("stage_hashes", {}).get(stage.name)
            changed_stages[stage.name] = (last_hash != stage_hash)
        
        # Update stored hashes
        self.build_history["stage_hashes"] = current_hashes
        
        return changed_stages
    
    def record_stage_changes(self, stages: List[Stage], image_tag: str):
        """Record stage changes for history tracking"""
        timestamp = datetime.now().isoformat()
        changed_stages = self.detect_stage_changes(stages)
        
        # Update stage history
        if "stage_history" not in self.build_history:
            self.build_history["stage_history"] = {}
        
        for stage_name, has_changed in changed_stages.items():
            if stage_name not in self.build_history["stage_history"]:
                self.build_history["stage_history"][stage_name] = []
            
            self.build_history["stage_history"][stage_name].append({
                "timestamp": timestamp,
                "changed": has_changed,
                "image_tag": image_tag
            })
        
        self._save_history()
        return changed_stages
    
    def get_stage_change_frequency(self, recent_builds: int = 10) -> Dict[str, float]:
        """Get change frequency for each stage over recent builds"""
        frequency = {}
        stage_history = self.build_history.get("stage_history", {})
        
        for stage_name, history in stage_history.items():
            if not history:
                frequency[stage_name] = 0.0
                continue
            
            recent_history = history[-recent_builds:] if len(history) > recent_builds else history
            changes = sum(1 for record in recent_history if record["changed"])
            frequency[stage_name] = changes / len(recent_history)
        
        return frequency
    
    def get_last_changed_stages(self, stages: List[Stage]) -> Set[str]:
        """Get stages that changed in the current build"""
        changed_stages = self.detect_stage_changes(stages)
        return {stage_name for stage_name, has_changed in changed_stages.items() if has_changed}
    
    def should_move_stage_to_end(self, stage_name: str, changed_stages: Set[str], 
                                current_order: List[str]) -> bool:
        """Determine if a stage should be moved to the end due to changes"""
        # If stage changed and it's not already at the end
        if stage_name in changed_stages:
            current_index = current_order.index(stage_name) if stage_name in current_order else -1
            if current_index < len(current_order) - 1:  # Not at the end
                return True
        
        # If stage is frequently changing (>50% of recent builds)
        frequency = self.get_stage_change_frequency().get(stage_name, 0.0)
        if frequency > 0.5:
            return True
        
        return False
    
    def get_optimized_stage_order(self, stages: List[Stage], base_order: List[str]) -> List[str]:
        """Get optimized stage order by moving frequently changed stages to the end"""
        changed_stages = self.get_last_changed_stages(stages)
        
        # Separate stable and unstable stages
        stable_stages = []
        unstable_stages = []
        
        for stage_name in base_order:
            if self.should_move_stage_to_end(stage_name, changed_stages, base_order):
                unstable_stages.append(stage_name)
            else:
                stable_stages.append(stage_name)
        
        # Return stable stages first, then unstable stages
        optimized_order = stable_stages + unstable_stages
        
        print(f"Stage reordering: {len(unstable_stages)} stages moved to end for optimization")
        if unstable_stages:
            print(f"Moved stages: {', '.join(unstable_stages)}")
        
        return optimized_order
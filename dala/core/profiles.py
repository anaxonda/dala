import os
import json
import re
from importlib import resources
from pathlib import Path
from typing import List, Optional
from ..models import log, SiteProfile

class ProfileManager:
    _instance = None
    def __init__(self, config_paths: List[str] = None):
        self.profiles: List[SiteProfile] = []
        if config_paths:
            for path in config_paths:
                self.load_config(path)
    @classmethod
    def get_instance(cls):
        if not cls._instance:
            cls._instance = cls()
            cls._instance.load_packaged_defaults()
            for path in [
                "sites.json",
                "sites.yaml",
                os.path.expanduser("~/.config/dala/sites.json"),
                os.path.expanduser("~/.config/dala/sites.yaml"),
            ]:
                cls._instance.load_config(path)
        return cls._instance

    def load_packaged_defaults(self):
        try:
            default_config = resources.files("dala.data").joinpath("sites.json")
            with resources.as_file(default_config) as path:
                self.load_config(str(path))
        except Exception as e:
            log.warning(f"Failed to load packaged site profiles: {e}")

    def _load_config_data(self, path: str):
        suffix = Path(path).suffix.lower()
        with open(path, 'r') as f:
            if suffix == ".json":
                return json.load(f)

            try:
                import yaml
            except ImportError:
                log.warning(
                    f"Skipping YAML config {path}: install PyYAML or use JSON config."
                )
                return None
            return yaml.safe_load(f)

    def load_config(self, path: str):
        if not os.path.exists(path): return
        try:
            data = self._load_config_data(path)
            if not data or not isinstance(data, list): return
            for item in data:
                self.profiles.append(SiteProfile(
                    name=item.get("name", "Unknown"),
                    domain_patterns=item.get("domains", []),
                    driver_alias=item.get("driver"),
                    content_selector=item.get("content_selector"),
                    remove_selectors=item.get("remove", []),
                    headers=item.get("headers", {}),
                    image_proxy_pattern=item.get("image_proxy_pattern")
                ))
            log.info(f"Loaded {len(data)} profiles from {path}")
        except Exception as e:
            log.warning(f"Failed to load config {path}: {e}")
    def get_profile(self, url: str) -> Optional[SiteProfile]:
        for p in reversed(self.profiles):
            for pattern in p.domain_patterns:
                try:
                    if re.search(pattern, url): return p
                except: pass
        return None

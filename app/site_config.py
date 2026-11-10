"""
The site itself, from its config folder (settings.CONFIG_DIR): its name,
description and paywalled sites (site.yaml), and what the summarizer asks its
LLM (prompts.yaml). Loaded once, on first use (get_site()).
"""
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from pydantic import BaseModel

from config import settings


class Prompts(BaseModel):
    summary: str  # System prompt for an article's summary
    same_story: str  # System prompt for two headlines: same story or not (an answer starting with "yes")
    subject: str  # System prompt for what a headline is about, in a few words (services/subjects.py)
    # By topic: a yes/no question put to the LLM about a feed item the tagger's model doubts (min_feed_score)
    topic_checks: Dict[str, str] = {}


class Site(BaseModel):
    name: str
    description: str
    paywalled_hosts: List[str] = []  # And their subdomains
    prompts: Prompts

    def is_paywalled(self, host: str) -> bool:
        return any(host == d or host.endswith("." + d) for d in self.paywalled_hosts)


def load_site(config_dir: Path) -> Site:
    site = yaml.safe_load((config_dir / "site.yaml").read_text())
    prompts = yaml.safe_load((config_dir / "prompts.yaml").read_text())
    return Site(**site, prompts=prompts)


_site: Optional[Site] = None


def get_site() -> Site:
    """The site's own settings, loaded from settings.CONFIG_DIR on first use."""
    global _site
    if _site is None:
        _site = load_site(Path(settings.CONFIG_DIR))
    return _site

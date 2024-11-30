import os
from dataclasses import fields
from typing import Any, Optional

from langchain_core.runnables import RunnableConfig
from dataclasses import dataclass
import config as cf


@dataclass(kw_only=True)
class Configuration:
    """The configurable fields for the chatbot."""
    username: str = cf.DEFAULT_USER
    league_id: str = cf.DEFAULT_LEAGUE_ID

    @classmethod
    def from_runnable_config(
        cls, config: Optional[RunnableConfig] = None
    ) -> "Configuration":
        """Create a Configuration instance from a RunnableConfig."""
        configurable = (
            config["configurable"] if config and "configurable" in config else {}
        )
        values: dict[str, Any] = {
            f.name: os.environ.get(f.name.upper(), configurable.get(f.name))
            for f in fields(cls)
            if f.init
        }
        return cls(**{k: v for k, v in values.items() if v})

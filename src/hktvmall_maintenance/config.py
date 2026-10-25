"""Site controls are public; account references and launch location are runtime configuration."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import Field

from .models import FrozenModel


class BrowserLaunchSpec(FrozenModel):
    locale: str = "en-GB"
    timezone_id: str = "UTC"
    accept_language: str = "en-GB,en;q=0.9"
    args: tuple[str, ...] = ()


BrowserLaunchConfig = BrowserLaunchSpec


class HktvmallTargetSpec(FrozenModel):
    key: Literal["hktvmall"]
    secret_ref: str = Field(default="", pattern=r"^(?:op://[^/\r\n{}]+/[^/\r\n{}]+)?$")
    login_url: Literal["https://www.hktvmall.com/hktv/en/login"]
    warm_url: Literal["https://www.hktvmall.com/hktv/en/"]
    launch: BrowserLaunchSpec

    def launch_config(self) -> BrowserLaunchConfig:
        return BrowserLaunchConfig(
            locale=self.launch.locale,
            timezone_id=self.launch.timezone_id,
            accept_language=self.launch.accept_language,
            args=self.launch.args,
        )


def load_identity_target(key: str, *, credential_reference: str = "", timezone_id: str | None = None):
    if key != "hktvmall":
        raise ValueError("unknown maintenance target")
    path = Path(__file__).parent / "definitions" / (key + ".toml")
    data = tomllib.loads(path.read_text())
    data["secret_ref"] = credential_reference
    if timezone_id is not None:
        data["launch"]["timezone_id"] = timezone_id
    return HktvmallTargetSpec.model_validate(data)

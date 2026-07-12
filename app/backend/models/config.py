import json
import re
from pathlib import Path
from typing import Self

from pydantic import BaseModel, model_validator

from backend.models.proxy_listing import ProxyListing
from backend.models.server_listing import ServerListing

SUBDOMAIN_REGEX: re.Pattern[str] = re.compile(r"(?:(?!-)[a-zA-Z0-9\-]+\.)*?(?!-)[a-zA-Z0-9\-]+")


class Config(BaseModel):
    proxy_listing: dict[str, ProxyListing]
    server_listing: dict[str, ServerListing]

    def validate_semantics(self) -> Self:
        # Important: repeated calls must not accumulate old errors
        for listing in self.proxy_listing.values():
            listing.errors.clear()

        proxy_ports: set[int] = set()
        for listing in self.proxy_listing.values():
            if listing.port in proxy_ports:
                listing.errors.append(f'Proxy port "{listing.port}" is already taken')
            for subdomain, server_name in listing.subdomains.items():
                if subdomain != "" and not SUBDOMAIN_REGEX.fullmatch(subdomain):
                    listing.errors.append(f'Proxy subdomain "{subdomain}" is invalid')
                if server_name not in self.server_listing:
                    listing.errors.append(
                        f'Proxy subdomain "{subdomain}" routes to non-existent '
                        f'server "{server_name}"'
                    )
            if listing.valid:
                proxy_ports.add(listing.port)
        return self

    @model_validator(mode="after")
    def _validate_semantics_on_model_creation(self) -> Self:
        self.validate_semantics()
        return self

    @classmethod
    def default(cls) -> Self:
        return cls(
            proxy_listing={"proxy1": ProxyListing(port=25565, enabled=True, subdomains={})},
            server_listing={},
        )

    @classmethod
    def load(cls, path: Path) -> Self:
        path.touch(exist_ok=True)
        if path.stat().st_size > 0:
            config = cls.model_validate(json.loads(path.read_text()), by_alias=True)
        else:
            config = cls.default()
            config.dump(path)
        return config

    def dump(self, path: Path) -> None:
        path.write_text(self.model_dump_json(indent=4, by_alias=True))

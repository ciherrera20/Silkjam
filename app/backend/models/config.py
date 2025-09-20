import re
import json
from pathlib import Path
from pydantic import BaseModel, model_validator
from .proxy_listing import ProxyListing
from .server_listing import ServerListing

SUBDOMAIN_REGEX: re.Pattern = re.compile(r"(?:(?!-)[a-zA-Z0-9\-]+\.)*?(?!-)[a-zA-Z0-9\-]+")

class Config(BaseModel):
    proxy_listing: dict[str, ProxyListing]
    server_listing: dict[str, ServerListing]

    @model_validator(mode="after")
    def validate_semantics(self):
        proxy_ports = set()
        for listing in self.proxy_listing.values():
            if listing.port in proxy_ports:
                listing.errors.append(f"Proxy port \"{listing.port}\" is already taken")
            for subdomain, server_name in listing.subdomains.items():
                if subdomain != "" and not SUBDOMAIN_REGEX.fullmatch(subdomain):
                    listing.errors.append(f"Proxy subdomain \"{subdomain}\" is invalid")
                if server_name not in self.server_listing:
                    listing.errors.append(f"Proxy subdomain \"{subdomain}\" routes to non-existent server \"{server_name}\"")
            if listing.valid:
                proxy_ports.add(listing.port)
        return self

    @classmethod
    def default(cls):
        return cls(
            proxy_listing={"proxy1": ProxyListing(
                port=25565,
                enabled=True,
                subdomains={}
            )},
            server_listing={}
        )

    @classmethod
    def load(cls, path: Path):
        path.touch(exist_ok=True)
        if path.stat().st_size > 0:
            config = cls.model_validate(json.loads(path.read_text()), by_alias=True)
        else:
            config = cls.default()
            config.dump(path)
        return config

    def dump(self, path: Path):
        path.write_text(self.model_dump_json(indent=4, by_alias=True))
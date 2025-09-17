import re
import json
from pathlib import Path
from pydantic import BaseModel, model_validator
from .proxy_listing import ProxyListing
from .server_listing import ServerListing

SUBDOMAIN_REGEX: re.Pattern = re.compile(r"(?:(?!-)[a-zA-Z0-9\-]+\.)*?(?!-)[a-zA-Z0-9\-]+")

class Config(BaseModel):
    proxy_listing: list[ProxyListing]
    server_listing: list[ServerListing]

    @model_validator(mode="after")
    def validate_semantics(self):
        proxy_names = set()
        proxy_ports = set()
        server_names = set()
        for listing in self.server_listing:
            if listing.name in server_names:
                listing.errors.append(f"Server name \"{listing.name}\" is already taken")
            if listing.valid:
                server_names.add(listing.name)
        for listing in self.proxy_listing:
            if listing.name in proxy_names:
                listing.errors.append(f"Proxy name \"{listing.name}\" is already taken")
            if listing.port in proxy_ports:
                listing.errors.append(f"Proxy port \"{listing.port}\" is already taken")
            for subdomain, server_name in listing.subdomains.items():
                if subdomain != "" and not SUBDOMAIN_REGEX.fullmatch(subdomain):
                    listing.errors.append(f"Proxy subdomain \"{subdomain}\" is invalid")
                if server_name not in server_names:
                    listing.errors.append(f"Proxy subdomain \"{subdomain}\" routes to non-existent server \"{server_name}\"")
            if listing.valid:
                proxy_names.add(listing.name)
                proxy_ports.add(listing.port)
        return self

    @classmethod
    def default(cls):
        return cls(
            proxy_listing=[ProxyListing(
                name="proxy1",
                port=25565,
                enabled=True,
                subdomains={}
            )],
            server_listing=[]
        )

    @classmethod
    def load(cls, path: Path):
        path.touch(exist_ok=True)
        if path.stat().st_size > 0:
            config = cls(**json.loads(path.read_text()))
        else:
            config = cls.default()
            config.dump(path)
        return config

    def dump(self, path: Path):
        path.write_text(self.model_dump_json(indent=4, by_alias=True))
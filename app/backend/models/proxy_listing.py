from typing import Annotated

from pydantic import BaseModel, Field, PrivateAttr

Port = Annotated[int, Field(gt=0, le=65535)]


class ProxyListing(BaseModel):
    port: Port
    enabled: bool
    subdomains: dict[str, str]

    # Annotate listing as valid or not
    _errors: list[str] = PrivateAttr(default_factory=list)

    @property
    def valid(self) -> bool:
        return len(self._errors) == 0

    @property
    def errors(self) -> list[str]:
        return self._errors

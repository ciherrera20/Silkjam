from typing import Annotated
from pydantic import BaseModel, PrivateAttr, Field

Port = Annotated[int, Field(gt=0, le=65535)]

class ProxyListing(BaseModel):
    port: Port
    enabled: bool
    subdomains: dict[str, str]

    # Annotate listing as valid or not
    _errors: list[str] = PrivateAttr(default_factory=list)

    @property
    def valid(self):
        return len(self._errors) == 0

    @property
    def errors(self):
        return self._errors

"""One immutable identity for the lifetime of this backend process."""
from uuid import uuid4


RUNTIME_INSTANCE_ID = uuid4()

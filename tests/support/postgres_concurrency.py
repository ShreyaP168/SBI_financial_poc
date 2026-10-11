"""Exercise simultaneous schema initialization on a disposable test database."""

import json
import os
from concurrent.futures import ThreadPoolExecutor

from underwriting_platform.infrastructure.jobs import RelationalJobRepository


def initialize(_):
    repository = RelationalJobRepository(os.environ["UW_DATABASE_URL"])
    repository.initialize()
    assert repository.ready()


if __name__ == "__main__":
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(initialize, range(24)))
    print(
        json.dumps(
            {
                "test": "postgres-concurrent-initialization",
                "status": "passed",
                "initializations": 24,
            }
        )
    )

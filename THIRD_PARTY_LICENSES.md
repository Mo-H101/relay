# Third-Party Licenses

Relay is licensed under the MIT License (see `LICENSE`). This file records
the licenses of the third-party packages Relay depends on, so the full
distribution remains permissive. Licenses were read from the installed
package metadata (`.dist-info` `METADATA`) in the development environment at
R2; full license texts ship inside each installed distribution.

## Runtime dependencies (shipped with the `relay` wheel)

| Package | License |
| --- | --- |
| fastapi | BSD-3-Clause |
| uvicorn | BSD-3-Clause |
| httpx | BSD-3-Clause |
| python-dotenv | BSD-3-Clause |
| pydantic | MIT |
| rich | MIT |
| textual | MIT |
| platformdirs | MIT |
| keyring | MIT |

## Build / test / tooling (not shipped)

| Package | License |
| --- | --- |
| setuptools | MIT |
| wheel | MIT |
| build | MIT |
| pytest | MIT |
| pytest-asyncio | Apache-2.0 |
| openai (test/benchmark harness) | Apache-2.0 |
| black | MIT |
| autopep8 | MIT |
| certifi | MPL-2.0 |

## Notable transitive dependencies

The transitive dependency tree (h11, idna, sniffio, packaging, etc.) is
entirely permissive (MIT / BSD / Apache-2.0). `relay migrate`/`pip install`
reports package metadata for verification at deploy time.

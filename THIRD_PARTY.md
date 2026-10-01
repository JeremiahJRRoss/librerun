# Third-party components

LibreRun is distributed as source. The components below are **not** in
this repository and the project does not redistribute them: building or
running LibreRun downloads them — pip and npm from their registries, the
container images from their registries, the spaCy model from GitHub — and
each keeps its own licence. They are listed so that anyone who builds,
runs or redistributes LibreRun knows what they are taking on.

Third-party material that *is* in the repository — Alembic's scaffold,
the Contributor Covenant and GitHub's gitignore template — is listed in
`NOTICE` and annotated in `REUSE.toml`.

**Where the facts come from.** Each licence is the one the component
declares — its PyPI metadata (the PEP 639 expression, else the licence
field and classifiers), its npm lockfile or registry entry, or its
upstream `LICENSE` — read on 2026-09-23 and normalised to SPDX where
that was unambiguous. It is the component's own statement, not a legal
review, and a component can change its licence between versions.

**Keeping it current.** `python3 scripts/check_licensing.py third-party`
fails when a lockfile, a requirements file, a manifest, a Dockerfile or
a compose file names a component that has no row here, and names the
missing rows. Add each from the component's registry entry.

**If you redistribute binaries.** An image or a wheel you build and hand
to someone else carries these components, and with them their notice
duties: licence texts, `NOTICE` files (for example, Apache-2.0 wheels
that ship one), and source availability for the MPL-2.0 and LGPL
components inside some wheels (numpy, pillow, setuptools, certifi and
others; see the notes). The project publishes no binary.

## Python packages

`backend/requirements.lock.txt` and `backend/adapters/requirements.lock.txt`
pin the backend's and the LangGraph adapter's versions, and
`services/gateway/requirements.lock.txt` the gateway's, each version with
the hash of every file PyPI serves for it (A3). The examples' requirements
are ranges; their rows record what resolved on 2026-09-23.

| Package | Version | Licence | Used by | Notes |
|---|---|---|---|---|
| aiofiles | 25.1.0 | Apache-2.0 | backend | its wheel ships a NOTICE file |
| aiohappyeyeballs | 2.7.1 | PSF-2.0 | gateway, LlamaIndex example |  |
| aiohttp | 3.14.3 | Apache-2.0 AND MIT | gateway, LlamaIndex example |  |
| aiosignal | 1.4.0 | Apache-2.0 | gateway, LlamaIndex example |  |
| aiosqlite | 0.22.1 | MIT | LlamaIndex example |  |
| alembic | 1.20.0 | MIT | backend |  |
| annotated-doc | 0.0.5 | MIT | backend, gateway |  |
| annotated-types | 0.8.0 | MIT | backend, gateway, LangGraph adapter, LlamaIndex example |  |
| anyio | 4.15.1 | MIT | backend, gateway, LangGraph adapter, LlamaIndex example, Python SDK |  |
| asgiref | 3.12.1 | BSD-3-Clause | backend |  |
| asyncpg | 0.31.0 | Apache-2.0 | backend, gateway |  |
| attrs | 26.1.0 | MIT | backend, gateway, LlamaIndex example |  |
| banks | 2.5.1 | MIT | LlamaIndex example |  |
| bcrypt | 4.0.1 | Apache-2.0 | backend |  |
| blis | 1.3.3 | BSD-3-Clause | backend, gateway | the package's licence metadata is ambiguous; this is the best reading of it |
| boto3 | 1.43.105 | Apache-2.0 | gateway | its wheel ships a NOTICE file |
| botocore | 1.43.105 | Apache-2.0 | gateway | ships an MPL-2.0 CA bundle and a NOTICE file; its wheel ships a NOTICE file |
| brotli | 1.2.0 | MIT | backend |  |
| brotlicffi | 1.2.0.2 | MIT | backend |  |
| build | 1.6.1 | MIT | CI release job |  |
| catalogue | 2.0.10 | MIT | backend, gateway |  |
| certifi | 2026.7.22 | MPL-2.0 | backend, gateway, LangGraph adapter, LlamaIndex example, Python SDK | reciprocal or unusual terms: read them before redistributing it |
| cffi | 2.1.1 | MIT-0 | backend, gateway |  |
| charset-normalizer | 3.5.1 | MIT | backend, gateway, LangGraph adapter, LlamaIndex example |  |
| click | 8.5.0 | BSD-3-Clause | backend, gateway, LlamaIndex example, Python SDK, echo example |  |
| cloudpathlib | 0.25.0 | MIT | backend, gateway |  |
| cloudpickle | 3.1.2 | BSD-3-Clause | LlamaIndex example |  |
| colorama | 0.4.6 | BSD-3-Clause | LlamaIndex example |  |
| confection | 1.3.3 | MIT | backend, gateway |  |
| cryptography | 48.0.1 | Apache-2.0 OR BSD-3-Clause | backend, gateway |  |
| cssselect2 | 0.10.1 | BSD-3-Clause | backend |  |
| cymem | 2.0.13 | MIT | backend, gateway |  |
| dataclasses-json | 0.6.7 | MIT | LlamaIndex example |  |
| defusedxml | 0.7.1 | PSF-2.0 | LlamaIndex example |  |
| deprecated | 1.3.1 | MIT | LlamaIndex example |  |
| dirtyjson | 1.0.8 | MIT OR AFL-2.1 | LlamaIndex example | the package's licence metadata is ambiguous; this is the best reading of it |
| distro | 1.9.0 | Apache-2.0 | gateway, LangGraph adapter, LlamaIndex example |  |
| dnspython | 2.8.0 | ISC | backend |  |
| ecdsa | 0.19.2 | MIT | backend |  |
| email-validator | 2.3.0 | Unlicense | backend |  |
| fastapi | 0.141.1 | MIT | backend, gateway |  |
| fastuuid | 0.14.0 | BSD-3-Clause | gateway |  |
| filelock | 3.32.7 | MIT | backend |  |
| filelock | 4.0.1 | MIT | gateway |  |
| filetype | 1.2.0 | MIT | LlamaIndex example |  |
| fonttools | 4.65.0 | MIT | backend |  |
| frozenlist | 1.8.0 | Apache-2.0 | gateway, LlamaIndex example |  |
| fsspec | 2026.9.0 | BSD-3-Clause | gateway, LlamaIndex example |  |
| google-auth | 2.58.0 | Apache-2.0 | backend |  |
| googleapis-common-protos | 1.75.3 | Apache-2.0 | backend, gateway |  |
| greenlet | 3.5.6 | MIT AND PSF-2.0 | backend, gateway, LlamaIndex example |  |
| griffe | 2.3.0 | ISC | LlamaIndex example |  |
| griffecli | 2.3.0 | ISC | LlamaIndex example |  |
| griffelib | 2.3.0 | ISC | LlamaIndex example |  |
| grpcio | 1.84.0 | Apache-2.0 | backend, gateway | ships an MPL-2.0 CA bundle |
| h11 | 0.16.0 | MIT | backend, gateway, LangGraph adapter, LlamaIndex example, Python SDK, echo example |  |
| h2 | 4.4.1 | MIT | backend |  |
| hf-xet | 1.6.0 | Apache-2.0 | gateway | links two MPL-2.0 Rust crates |
| hiredis | 3.4.1 | MIT | backend |  |
| hiredis | 3.4.2 | MIT | gateway |  |
| hpack | 4.2.0 | MIT | backend |  |
| httpcore | 1.0.9 | BSD-3-Clause | backend, gateway, LangGraph adapter, LlamaIndex example, Python SDK |  |
| httpcore2 | 2.13.0 | BSD-3-Clause | LangGraph adapter |  |
| httptools | 0.8.0 | MIT | backend, gateway |  |
| httpx | 0.28.1 | BSD-3-Clause | backend, gateway, LangGraph adapter, LlamaIndex example, Python SDK |  |
| httpx2 | 2.13.0 | BSD-3-Clause | LangGraph adapter |  |
| httpx2-jsfetch | 1.0 | BSD-3-Clause | LangGraph adapter |  |
| huggingface-hub | 1.33.0 | Apache-2.0 | gateway |  |
| hyperframe | 6.1.0 | MIT | backend |  |
| idna | 3.19 | BSD-3-Clause | backend, LangGraph adapter |  |
| idna | 3.20 | BSD-3-Clause | gateway, LlamaIndex example, Python SDK |  |
| importlib-metadata | 8.9.0 | Apache-2.0 | gateway |  |
| iniconfig | 2.3.0 | MIT | backend, gateway, Python SDK |  |
| jinja2 | 3.1.6 | BSD-3-Clause | backend, gateway, LlamaIndex example |  |
| jiter | 0.17.0 | MIT | gateway, LlamaIndex example |  |
| jmespath | 1.1.0 | MIT | gateway |  |
| joblib | 1.6.0 | BSD-3-Clause | LlamaIndex example |  |
| jsonpatch | 1.33 | BSD-3-Clause | LangGraph adapter |  |
| jsonpointer | 3.1.1 | BSD-3-Clause | LangGraph adapter |  |
| jsonschema | 4.26.0 | MIT | backend, gateway |  |
| jsonschema-specifications | 2025.9.1 | MIT | backend, gateway |  |
| langchain-core | 1.6.3 | MIT | LangGraph adapter |  |
| langchain-protocol | 0.0.19 | MIT | LangGraph adapter |  |
| langgraph | 1.2.2 | MIT | LangGraph adapter |  |
| langgraph-checkpoint | 4.2.0 | MIT | LangGraph adapter |  |
| langgraph-prebuilt | 1.1.0 | MIT | LangGraph adapter |  |
| langgraph-sdk | 0.3.15 | MIT | LangGraph adapter |  |
| langsmith | 0.12.5 | MIT | LangGraph adapter |  |
| litellm | 1.103.1 | MIT | gateway | its licence reserves an `enterprise/` directory for a commercial licence; that directory is not in the 1.103.1 wheel, whose three paths naming `enterprise` are MIT package content under `litellm/proxy/`. The gateway pins it exactly and uses it as a library alone (A3) |
| llama-index-core | 0.14.24 | MIT | LlamaIndex example |  |
| llama-index-instrumentation | 0.6.0 | MIT | LlamaIndex example |  |
| llama-index-llms-openai | 0.7.10 | MIT | LlamaIndex example |  |
| llama-index-llms-openai-like | 0.8.0 | MIT | LlamaIndex example |  |
| llama-index-workflows | 2.24.1 | MIT | LlamaIndex example |  |
| mako | 1.4.1 | MIT | backend |  |
| markdown-it-py | 4.2.0 | MIT | backend, gateway |  |
| markupsafe | 3.0.3 | BSD-3-Clause | backend, gateway, LlamaIndex example |  |
| marshmallow | 3.26.2 | MIT | LlamaIndex example |  |
| mdurl | 0.1.2 | MIT | backend, gateway |  |
| msal | 1.38.0 | MIT | backend |  |
| msgspec | 0.21.1 | BSD-3-Clause | backend |  |
| multidict | 6.9.1 | Apache-2.0 | gateway, LlamaIndex example |  |
| murmurhash | 1.0.15 | MIT | backend, gateway |  |
| mypy-extensions | 1.1.0 | MIT | LlamaIndex example |  |
| nest-asyncio | 1.6.0 | BSD-2-Clause | LlamaIndex example | the package's licence metadata is ambiguous; this is the best reading of it |
| networkx | 3.7 | BSD-3-Clause | LlamaIndex example |  |
| nltk | 3.10.3 | Apache-2.0 | LlamaIndex example |  |
| numpy | 2.4.6 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | backend, gateway | its wheels bundle libgfortran (GPL-3.0-or-later WITH GCC-exception-3.1) and libquadmath (LGPL-2.1-or-later) |
| numpy | 2.5.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | LlamaIndex example | its wheels bundle libgfortran (GPL-3.0-or-later WITH GCC-exception-3.1) and libquadmath (LGPL-2.1-or-later) |
| openai | 2.54.0 | Apache-2.0 | gateway, LlamaIndex example |  |
| openinference-instrumentation | 0.1.65 | Apache-2.0 | backend |  |
| openinference-semantic-conventions | 0.1.38 | Apache-2.0 | backend |  |
| opentelemetry-api | 1.44.0 | Apache-2.0 | backend, gateway, LangGraph adapter, LlamaIndex example, Python SDK, echo example |  |
| opentelemetry-exporter-otlp | 1.44.0 | Apache-2.0 | backend, gateway |  |
| opentelemetry-exporter-otlp-proto-common | 1.44.0 | Apache-2.0 | backend, gateway, LlamaIndex example, Python SDK, echo example |  |
| opentelemetry-exporter-otlp-proto-grpc | 1.44.0 | Apache-2.0 | backend, gateway |  |
| opentelemetry-exporter-otlp-proto-http | 1.44.0 | Apache-2.0 | backend, gateway |  |
| opentelemetry-instrumentation | 0.65b0 | Apache-2.0 | backend |  |
| opentelemetry-instrumentation-asgi | 0.65b0 | Apache-2.0 | backend |  |
| opentelemetry-instrumentation-fastapi | 0.65b0 | Apache-2.0 | backend |  |
| opentelemetry-proto | 1.44.0 | Apache-2.0 | backend, gateway, LlamaIndex example, Python SDK, echo example |  |
| opentelemetry-sdk | 1.44.0 | Apache-2.0 | backend, gateway, LlamaIndex example, Python SDK, echo example |  |
| opentelemetry-semantic-conventions | 0.65b0 | Apache-2.0 | backend, gateway, LlamaIndex example, Python SDK, echo example |  |
| opentelemetry-util-http | 0.65b0 | Apache-2.0 | backend |  |
| orjson | 3.12.0 | MPL-2.0 AND (Apache-2.0 OR MIT) | backend, LangGraph adapter | reciprocal or unusual terms: read them before redistributing it |
| ormsgpack | 1.12.2 | Apache-2.0 OR MIT | LangGraph adapter |  |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | backend, gateway, LangGraph adapter, LlamaIndex example, Python SDK, CI release job |  |
| passlib | 1.7.4 | BSD-3-Clause | backend | BSD, with Beerware, ISC and UnixCrypt notices for bundled code; the package's licence metadata is ambiguous; this is the best reading of it |
| phonenumbers | 9.0.39 | Apache-2.0 | backend, gateway |  |
| pillow | 12.3.0 | MIT-CMU | backend, LlamaIndex example | its wheels compile in an LGPL-2.1-or-later fribidi shim and bundle FreeType (FTL credit clause) |
| pinecone | 10.0.0 | Apache-2.0 | backend |  |
| platformdirs | 4.11.12 | MIT | LlamaIndex example |  |
| pluggy | 1.6.0 | MIT | backend, gateway, Python SDK |  |
| preshed | 3.0.13 | MIT | backend, gateway |  |
| presidio-analyzer | 2.2.364 | MIT | backend, gateway |  |
| presidio-anonymizer | 2.2.364 | MIT | backend, gateway |  |
| propcache | 0.5.4 | Apache-2.0 | gateway, LlamaIndex example | its wheel ships a NOTICE file |
| protobuf | 7.36.1 | BSD-3-Clause | backend |  |
| protobuf | 7.36.2 | BSD-3-Clause | gateway, LlamaIndex example, Python SDK, echo example |  |
| pyasn1 | 0.6.4 | BSD-2-Clause | backend |  |
| pyasn1-modules | 0.4.2 | BSD-2-Clause | backend | the package's licence metadata is ambiguous; this is the best reading of it |
| pycparser | 3.0 | BSD-3-Clause | backend, gateway |  |
| pydantic | 2.13.5 | MIT | backend, gateway, LangGraph adapter, LlamaIndex example |  |
| pydantic-core | 2.46.5 | MIT | backend, gateway, LangGraph adapter, LlamaIndex example |  |
| pydantic-settings | 2.15.0 | MIT | backend, gateway |  |
| pydyf | 0.12.1 | BSD-3-Clause | backend |  |
| pygments | 2.21.0 | BSD-2-Clause | backend, gateway, Python SDK |  |
| pyjwt | 2.14.0 | MIT | backend |  |
| pyphen | 0.18.1 | GPL-2.0-or-later OR LGPL-2.1-or-later OR MPL-1.1 | backend | bundles about 50 hyphenation dictionaries under their own terms (some LPPL, one CC-BY-SA-4.0 OR LGPL-3.0, 35 with no stated licence); reciprocal or unusual terms: read them before redistributing it |
| pyproject-hooks | 1.3.3 | MIT | CI release job |  |
| pytest | 9.1.1 | MIT | backend, gateway, Python SDK |  |
| pytest-asyncio | 1.4.0 | Apache-2.0 | backend, gateway, Python SDK |  |
| python-dateutil | 2.9.0.post0 | Apache-2.0 AND BSD-3-Clause | gateway | dual-licensed (Apache-2.0 for newer code, BSD-3-Clause for older); the package's licence metadata is ambiguous; this is the best reading of it |
| python-dotenv | 1.2.3 | BSD-3-Clause | backend, gateway |  |
| python-jose | 3.5.0 | MIT | backend |  |
| python-multipart | 0.0.32 | Apache-2.0 | backend |  |
| pyyaml | 6.0.3 | MIT | backend, gateway, LangGraph adapter, LlamaIndex example |  |
| redis | 8.1.0 | MIT | backend, gateway |  |
| referencing | 0.37.0 | MIT | backend, gateway |  |
| regex | 2026.9.10 | Apache-2.0 AND CNRI-Python | backend, gateway, LlamaIndex example | reciprocal or unusual terms: read them before redistributing it |
| requests | 2.34.2 | Apache-2.0 | backend, gateway, LangGraph adapter, LlamaIndex example | its wheel ships a NOTICE file |
| requests-file | 3.0.1 | Apache-2.0 | backend, gateway |  |
| requests-toolbelt | 1.0.0 | Apache-2.0 | LangGraph adapter |  |
| rich | 15.0.0 | MIT | backend, gateway |  |
| rpds-py | 2026.6.3 | MIT | backend, gateway |  |
| rsa | 4.9.1 | Apache-2.0 | backend |  |
| s3transfer | 0.19.2 | Apache-2.0 | gateway | its wheel ships a NOTICE file |
| setuptools | 84.0.0 | MIT | backend, gateway, LlamaIndex example, Python SDK, CLI | vendors autocommand (LGPL-3.0) and validate-pyproject files (MPL-2.0); ships two attribution NOTICE files; its wheel ships a NOTICE file |
| shellingham | 1.5.4 | ISC | backend, gateway |  |
| six | 1.17.0 | MIT | backend, gateway |  |
| smart-open | 8.0.1 | MIT | backend, gateway |  |
| sniffio | 1.3.1 | MIT OR Apache-2.0 | gateway, LangGraph adapter, LlamaIndex example |  |
| spacy | 3.8.16 | MIT | backend, gateway |  |
| spacy-legacy | 3.0.12 | MIT | backend, gateway |  |
| spacy-loggers | 1.0.5 | MIT | backend, gateway |  |
| sqlalchemy | 2.0.54 | MIT | backend, gateway, LlamaIndex example |  |
| srsly | 2.5.3 | MIT | backend, gateway |  |
| starlette | 1.6.0 | BSD-3-Clause | backend |  |
| starlette | 1.7.0 | BSD-3-Clause | gateway |  |
| structlog | 26.1.0 | MIT OR Apache-2.0 | backend, gateway | its wheel ships a NOTICE file |
| tavily-python | 0.8.3 | MIT | backend | declared but never imported |
| tenacity | 9.1.4 | Apache-2.0 | LangGraph adapter, LlamaIndex example |  |
| thinc | 8.3.13 | MIT | backend, gateway |  |
| tiktoken | 0.14.0 | MIT | backend, gateway, LlamaIndex example | declared but never imported |
| tinycss2 | 1.5.1 | BSD-3-Clause | backend |  |
| tinyhtml5 | 2.1.0 | MIT | backend |  |
| tinytag | 2.3.2 | MIT | LlamaIndex example |  |
| tldextract | 5.3.2 | BSD-3-Clause | backend, gateway | embeds the Public Suffix List (MPL-2.0) |
| tokenizers | 0.23.2 | Apache-2.0 | gateway |  |
| tqdm | 4.70.1 | MPL-2.0 AND MIT | backend, gateway, LlamaIndex example | reciprocal or unusual terms: read them before redistributing it |
| truststore | 0.10.4 | MIT | LangGraph adapter |  |
| typer | 0.27.2 | MIT | backend, gateway |  |
| typing-extensions | 4.16.0 | PSF-2.0 | backend, gateway, LangGraph adapter, LlamaIndex example, Python SDK, echo example |  |
| typing-inspect | 0.9.0 | MIT | LlamaIndex example |  |
| typing-inspection | 0.4.4 | MIT | backend, gateway, LangGraph adapter, LlamaIndex example |  |
| urllib3 | 2.8.0 | MIT | backend, gateway, LangGraph adapter, LlamaIndex example |  |
| uuid-utils | 0.17.1 | BSD-3-Clause | LangGraph adapter |  |
| uvicorn | 0.53.0 | BSD-3-Clause | backend, gateway, LlamaIndex example, Python SDK, echo example |  |
| uvloop | 0.22.1 | MIT OR Apache-2.0 | backend, gateway |  |
| wasabi | 1.1.3 | MIT | backend, gateway |  |
| watchfiles | 1.2.0 | MIT | backend |  |
| watchfiles | 1.3.0 | MIT | gateway |  |
| weasel | 1.0.0 | MIT | backend, gateway |  |
| weasyprint | 70.0 | BSD-3-Clause | backend |  |
| webencodings | 0.6.1 | BSD-3-Clause | backend |  |
| websockets | 17.1 | BSD-3-Clause | backend, gateway, LangGraph adapter |  |
| wrapt | 2.4.1 | BSD-2-Clause | backend, gateway, LlamaIndex example |  |
| xxhash | 4.0.1 | BSD-2-Clause | LangGraph adapter |  |
| yarl | 1.25.1 | Apache-2.0 | gateway, LlamaIndex example | its wheel ships a NOTICE file |
| zipp | 4.1.0 | MIT | gateway |  |
| zopfli | 0.4.3 | Apache-2.0 | backend |  |
| zstandard | 0.25.0 | BSD-3-Clause | LangGraph adapter |  |

## Models and data fetched at build or run time

| Model or data | Version | Licence | Fetched by | Notes |
|---|---|---|---|---|
| en_core_web_lg (spaCy model) | 3.8.0 | MIT | `backend/Dockerfile` and `services/gateway/Dockerfile` (`python -m spacy download`), from GitHub | trained on OntoNotes 5; the WordNet 3.0 notice applies to its lexical data |
| LiteLLM model cost map | the copy in LiteLLM's wheel, 1.103.1 | MIT | nothing: the gateway image sets `LITELLM_LOCAL_MODEL_COST_MAP=True` (A3), so LiteLLM reads the `model_prices_and_context_window_backup.json` its wheel carries | part of LiteLLM. A gateway run outside the image, without that variable, fetches the map from GitHub at start-up |
| Public Suffix List | current | MPL-2.0 | `tldextract`, refreshed at run time | |

## npm packages

The web UI's versions are pinned by `frontend/package-lock.json`. The
`container-ts` template and the Vercel AI example have no lockfile; their
rows record what resolved on 2026-09-23. "Ships" says where a package
ends up in a build: the browser bundle a user's browser downloads, the
web image's server, or only the build and the tests.

| Package | Version | Licence | Used by | Ships |
|---|---|---|---|---|
| @ai-sdk/gateway | 4.0.87 | Apache-2.0 | Vercel AI example (unlocked) | web image |
| @ai-sdk/openai | 4.0.71 | Apache-2.0 | Vercel AI example (unlocked) | web image |
| @ai-sdk/provider | 4.0.17 | Apache-2.0 | Vercel AI example (unlocked) | web image |
| @ai-sdk/provider-utils | 5.0.45 | Apache-2.0 | Vercel AI example (unlocked) | web image |
| @esbuild/aix-ppc64 | 0.28.2 | MIT | Vercel AI example (unlocked) | build and test only |
| @esbuild/android-arm | 0.28.2 | MIT | Vercel AI example (unlocked) | build and test only |
| @esbuild/android-arm64 | 0.28.2 | MIT | Vercel AI example (unlocked) | build and test only |
| @esbuild/android-x64 | 0.28.2 | MIT | Vercel AI example (unlocked) | build and test only |
| @esbuild/darwin-arm64 | 0.28.2 | MIT | Vercel AI example (unlocked) | build and test only |
| @esbuild/darwin-x64 | 0.28.2 | MIT | Vercel AI example (unlocked) | build and test only |
| @esbuild/freebsd-arm64 | 0.28.2 | MIT | Vercel AI example (unlocked) | build and test only |
| @esbuild/freebsd-x64 | 0.28.2 | MIT | Vercel AI example (unlocked) | build and test only |
| @esbuild/linux-arm | 0.28.2 | MIT | Vercel AI example (unlocked) | build and test only |
| @esbuild/linux-arm64 | 0.28.2 | MIT | Vercel AI example (unlocked) | build and test only |
| @esbuild/linux-ia32 | 0.28.2 | MIT | Vercel AI example (unlocked) | build and test only |
| @esbuild/linux-loong64 | 0.28.2 | MIT | Vercel AI example (unlocked) | build and test only |
| @esbuild/linux-mips64el | 0.28.2 | MIT | Vercel AI example (unlocked) | build and test only |
| @esbuild/linux-ppc64 | 0.28.2 | MIT | Vercel AI example (unlocked) | build and test only |
| @esbuild/linux-riscv64 | 0.28.2 | MIT | Vercel AI example (unlocked) | build and test only |
| @esbuild/linux-s390x | 0.28.2 | MIT | Vercel AI example (unlocked) | build and test only |
| @esbuild/linux-x64 | 0.28.2 | MIT | Vercel AI example (unlocked) | build and test only |
| @esbuild/netbsd-arm64 | 0.28.2 | MIT | Vercel AI example (unlocked) | build and test only |
| @esbuild/netbsd-x64 | 0.28.2 | MIT | Vercel AI example (unlocked) | build and test only |
| @esbuild/openbsd-arm64 | 0.28.2 | MIT | Vercel AI example (unlocked) | build and test only |
| @esbuild/openbsd-x64 | 0.28.2 | MIT | Vercel AI example (unlocked) | build and test only |
| @esbuild/openharmony-arm64 | 0.28.2 | MIT | Vercel AI example (unlocked) | build and test only |
| @esbuild/sunos-x64 | 0.28.2 | MIT | Vercel AI example (unlocked) | build and test only |
| @esbuild/win32-arm64 | 0.28.2 | MIT | Vercel AI example (unlocked) | build and test only |
| @esbuild/win32-ia32 | 0.28.2 | MIT | Vercel AI example (unlocked) | build and test only |
| @esbuild/win32-x64 | 0.28.2 | MIT | Vercel AI example (unlocked) | build and test only |
| @standard-schema/spec | 1.1.0 | MIT | Vercel AI example (unlocked) | web image |
| @types/node | 22.20.0 | MIT | Vercel AI example (unlocked) | build and test only |
| @vercel/oidc | 3.2.0 | Apache-2.0 | Vercel AI example (unlocked) | web image |
| @workflow/serde | 4.1.0 | Apache-2.0 | Vercel AI example (unlocked) | web image |
| ai | 7.0.107 | Apache-2.0 | Vercel AI example (unlocked) | web image |
| esbuild | 0.28.2 | MIT | Vercel AI example (unlocked) | web image |
| eventsource-parser | 3.1.1 | MIT | Vercel AI example (unlocked) | web image |
| fsevents | 2.3.3 | MIT | Vercel AI example (unlocked) | build and test only |
| json-schema | 0.4.0 | (AFL-2.1 OR BSD-3-Clause) | Vercel AI example (unlocked) | web image |
| tsx | 4.23.15 | MIT | Vercel AI example (unlocked) | web image |
| typescript | 5.9.3 | Apache-2.0 | Vercel AI example (unlocked) | build and test only |
| undici | 7.29.1 | MIT | Vercel AI example (unlocked) | web image |
| undici-types | 6.21.0 | MIT | Vercel AI example (unlocked) | build and test only |
| zod | 4.6.5 | MIT | Vercel AI example (unlocked) | web image |
| @ai-sdk/gateway | 4.0.87 | Apache-2.0 | container-ts template (unlocked) | build and test only |
| @ai-sdk/openai | 4.0.71 | Apache-2.0 | container-ts template (unlocked) | build and test only |
| @ai-sdk/provider | 4.0.17 | Apache-2.0 | container-ts template (unlocked) | build and test only |
| @ai-sdk/provider-utils | 5.0.45 | Apache-2.0 | container-ts template (unlocked) | build and test only |
| @esbuild/aix-ppc64 | 0.28.2 | MIT | container-ts template (unlocked) | build and test only |
| @esbuild/android-arm | 0.28.2 | MIT | container-ts template (unlocked) | build and test only |
| @esbuild/android-arm64 | 0.28.2 | MIT | container-ts template (unlocked) | build and test only |
| @esbuild/android-x64 | 0.28.2 | MIT | container-ts template (unlocked) | build and test only |
| @esbuild/darwin-arm64 | 0.28.2 | MIT | container-ts template (unlocked) | build and test only |
| @esbuild/darwin-x64 | 0.28.2 | MIT | container-ts template (unlocked) | build and test only |
| @esbuild/freebsd-arm64 | 0.28.2 | MIT | container-ts template (unlocked) | build and test only |
| @esbuild/freebsd-x64 | 0.28.2 | MIT | container-ts template (unlocked) | build and test only |
| @esbuild/linux-arm | 0.28.2 | MIT | container-ts template (unlocked) | build and test only |
| @esbuild/linux-arm64 | 0.28.2 | MIT | container-ts template (unlocked) | build and test only |
| @esbuild/linux-ia32 | 0.28.2 | MIT | container-ts template (unlocked) | build and test only |
| @esbuild/linux-loong64 | 0.28.2 | MIT | container-ts template (unlocked) | build and test only |
| @esbuild/linux-mips64el | 0.28.2 | MIT | container-ts template (unlocked) | build and test only |
| @esbuild/linux-ppc64 | 0.28.2 | MIT | container-ts template (unlocked) | build and test only |
| @esbuild/linux-riscv64 | 0.28.2 | MIT | container-ts template (unlocked) | build and test only |
| @esbuild/linux-s390x | 0.28.2 | MIT | container-ts template (unlocked) | build and test only |
| @esbuild/linux-x64 | 0.28.2 | MIT | container-ts template (unlocked) | build and test only |
| @esbuild/netbsd-arm64 | 0.28.2 | MIT | container-ts template (unlocked) | build and test only |
| @esbuild/netbsd-x64 | 0.28.2 | MIT | container-ts template (unlocked) | build and test only |
| @esbuild/openbsd-arm64 | 0.28.2 | MIT | container-ts template (unlocked) | build and test only |
| @esbuild/openbsd-x64 | 0.28.2 | MIT | container-ts template (unlocked) | build and test only |
| @esbuild/openharmony-arm64 | 0.28.2 | MIT | container-ts template (unlocked) | build and test only |
| @esbuild/sunos-x64 | 0.28.2 | MIT | container-ts template (unlocked) | build and test only |
| @esbuild/win32-arm64 | 0.28.2 | MIT | container-ts template (unlocked) | build and test only |
| @esbuild/win32-ia32 | 0.28.2 | MIT | container-ts template (unlocked) | build and test only |
| @esbuild/win32-x64 | 0.28.2 | MIT | container-ts template (unlocked) | build and test only |
| @standard-schema/spec | 1.1.0 | MIT | container-ts template (unlocked) | build and test only |
| @types/node | 22.20.0 | MIT | container-ts template (unlocked) | build and test only |
| @vercel/oidc | 3.2.0 | Apache-2.0 | container-ts template (unlocked) | build and test only |
| @workflow/serde | 4.1.0 | Apache-2.0 | container-ts template (unlocked) | build and test only |
| ai | 7.0.107 | Apache-2.0 | container-ts template (unlocked) | build and test only |
| esbuild | 0.28.2 | MIT | container-ts template (unlocked) | build and test only |
| eventsource-parser | 3.1.1 | MIT | container-ts template (unlocked) | build and test only |
| fsevents | 2.3.3 | MIT | container-ts template (unlocked) | build and test only |
| json-schema | 0.4.0 | (AFL-2.1 OR BSD-3-Clause) | container-ts template (unlocked) | build and test only |
| tsx | 4.23.15 | MIT | container-ts template (unlocked) | build and test only |
| typescript | 5.9.3 | Apache-2.0 | container-ts template (unlocked) | build and test only |
| undici | 7.29.1 | MIT | container-ts template (unlocked) | build and test only |
| undici-types | 6.21.0 | MIT | container-ts template (unlocked) | build and test only |
| zod | 4.6.5 | MIT | container-ts template (unlocked) | build and test only |
| @alloc/quick-lru | 5.2.0 | MIT | web UI (locked) | build and test only |
| @asamuzakjp/css-color | 3.2.0 | MIT | web UI (locked) | build and test only |
| @azure/msal-browser | 3.30.0 | MIT | web UI (locked) | build and test only |
| @azure/msal-common | 14.16.1 | MIT | web UI (locked) | build and test only |
| @azure/msal-react | 2.2.0 | MIT | web UI (locked) | build and test only |
| @babel/code-frame | 7.29.7 | MIT | web UI (locked) | build and test only |
| @babel/helper-validator-identifier | 7.29.7 | MIT | web UI (locked) | build and test only |
| @babel/runtime | 7.29.7 | MIT | web UI (locked) | build and test only |
| @csstools/color-helpers | 5.1.0 | MIT-0 | web UI (locked) | build and test only |
| @csstools/css-calc | 2.1.4 | MIT | web UI (locked) | build and test only |
| @csstools/css-color-parser | 3.1.0 | MIT | web UI (locked) | build and test only |
| @csstools/css-parser-algorithms | 3.0.5 | MIT | web UI (locked) | build and test only |
| @csstools/css-tokenizer | 3.0.4 | MIT | web UI (locked) | build and test only |
| @emnapi/core | 1.9.2 | MIT | web UI (locked) | build and test only |
| @emnapi/runtime | 1.9.2 | MIT | web UI (locked) | build and test only |
| @emnapi/wasi-threads | 1.2.1 | MIT | web UI (locked) | build and test only |
| @eslint-community/eslint-utils | 4.9.1 | MIT | web UI (locked) | build and test only |
| @eslint-community/regexpp | 4.12.2 | MIT | web UI (locked) | build and test only |
| @eslint/eslintrc | 2.1.4 | MIT | web UI (locked) | build and test only |
| @eslint/js | 8.57.1 | MIT | web UI (locked) | build and test only |
| @humanwhocodes/config-array | 0.13.0 | Apache-2.0 | web UI (locked) | build and test only |
| @humanwhocodes/module-importer | 1.0.1 | Apache-2.0 | web UI (locked) | build and test only |
| @humanwhocodes/object-schema | 2.0.3 | BSD-3-Clause | web UI (locked) | build and test only |
| @isaacs/cliui | 8.0.2 | ISC | web UI (locked) | build and test only |
| @jridgewell/gen-mapping | 0.3.13 | MIT | web UI (locked) | build and test only |
| @jridgewell/resolve-uri | 3.1.2 | MIT | web UI (locked) | build and test only |
| @jridgewell/sourcemap-codec | 1.5.5 | MIT | web UI (locked) | build and test only |
| @jridgewell/trace-mapping | 0.3.31 | MIT | web UI (locked) | build and test only |
| @napi-rs/wasm-runtime | 0.2.12 | MIT | web UI (locked) | build and test only |
| @next/env | 14.2.5 | MIT | web UI (locked) | web image |
| @next/eslint-plugin-next | 14.2.5 | MIT | web UI (locked) | build and test only |
| @next/swc-darwin-arm64 | 14.2.5 | MIT | web UI (locked) | build and test only |
| @next/swc-darwin-x64 | 14.2.5 | MIT | web UI (locked) | build and test only |
| @next/swc-linux-arm64-gnu | 14.2.5 | MIT | web UI (locked) | build and test only |
| @next/swc-linux-arm64-musl | 14.2.5 | MIT | web UI (locked) | build and test only |
| @next/swc-linux-x64-gnu | 14.2.5 | MIT | web UI (locked) | build and test only |
| @next/swc-linux-x64-musl | 14.2.5 | MIT | web UI (locked) | build and test only |
| @next/swc-win32-arm64-msvc | 14.2.5 | MIT | web UI (locked) | build and test only |
| @next/swc-win32-ia32-msvc | 14.2.5 | MIT | web UI (locked) | build and test only |
| @next/swc-win32-x64-msvc | 14.2.5 | MIT | web UI (locked) | build and test only |
| @nodelib/fs.scandir | 2.1.5 | MIT | web UI (locked) | build and test only |
| @nodelib/fs.stat | 2.0.5 | MIT | web UI (locked) | build and test only |
| @nodelib/fs.walk | 1.2.8 | MIT | web UI (locked) | build and test only |
| @nolyfill/is-core-module | 1.0.39 | MIT | web UI (locked) | build and test only |
| @oxc-project/types | 0.146.0 | MIT | web UI (locked) | build and test only |
| @pkgjs/parseargs | 0.11.0 | MIT | web UI (locked) | build and test only |
| @playwright/test | 1.56.1 | Apache-2.0 | web UI (locked) | build and test only |
| @react-oauth/google | 0.12.2 | MIT | web UI (locked) | build and test only |
| @rolldown/binding-android-arm-eabi | 1.2.5 | MIT | web UI (locked) | build and test only |
| @rolldown/binding-android-arm64 | 1.2.5 | MIT | web UI (locked) | build and test only |
| @rolldown/binding-darwin-arm64 | 1.2.5 | MIT | web UI (locked) | build and test only |
| @rolldown/binding-darwin-x64 | 1.2.5 | MIT | web UI (locked) | build and test only |
| @rolldown/binding-freebsd-x64 | 1.2.5 | MIT | web UI (locked) | build and test only |
| @rolldown/binding-linux-arm-gnueabihf | 1.2.5 | MIT | web UI (locked) | build and test only |
| @rolldown/binding-linux-arm64-gnu | 1.2.5 | MIT | web UI (locked) | build and test only |
| @rolldown/binding-linux-arm64-musl | 1.2.5 | MIT | web UI (locked) | build and test only |
| @rolldown/binding-linux-ppc64-gnu | 1.2.5 | MIT | web UI (locked) | build and test only |
| @rolldown/binding-linux-s390x-gnu | 1.2.5 | MIT | web UI (locked) | build and test only |
| @rolldown/binding-linux-x64-gnu | 1.2.5 | MIT | web UI (locked) | build and test only |
| @rolldown/binding-linux-x64-musl | 1.2.5 | MIT | web UI (locked) | build and test only |
| @rolldown/binding-openharmony-arm64 | 1.2.5 | MIT | web UI (locked) | build and test only |
| @rolldown/binding-win32-arm64-msvc | 1.2.5 | MIT | web UI (locked) | build and test only |
| @rolldown/binding-win32-x64-msvc | 1.2.5 | MIT | web UI (locked) | build and test only |
| @rolldown/pluginutils | 1.0.1 | MIT | web UI (locked) | build and test only |
| @rtsao/scc | 1.1.0 | MIT | web UI (locked) | build and test only |
| @rushstack/eslint-patch | 1.16.1 | MIT | web UI (locked) | build and test only |
| @standard-schema/spec | 1.1.0 | MIT | web UI (locked) | build and test only |
| @swc/counter | 0.1.3 | Apache-2.0 | web UI (locked) | build and test only |
| @swc/helpers | 0.5.5 | Apache-2.0 | web UI (locked) | web image |
| @testing-library/dom | 10.4.2 | MIT | web UI (locked) | build and test only |
| @testing-library/react | 16.3.3 | MIT | web UI (locked) | build and test only |
| @tybys/wasm-util | 0.10.1 | MIT | web UI (locked) | build and test only |
| @types/aria-query | 5.0.4 | MIT | web UI (locked) | build and test only |
| @types/chai | 5.2.3 | MIT | web UI (locked) | build and test only |
| @types/deep-eql | 4.0.2 | MIT | web UI (locked) | build and test only |
| @types/estree | 1.0.9 | MIT | web UI (locked) | build and test only |
| @types/json5 | 0.0.29 | MIT | web UI (locked) | build and test only |
| @types/node | 22.20.4 | MIT | web UI (locked) | build and test only |
| @types/prop-types | 15.7.15 | MIT | web UI (locked) | build and test only |
| @types/react | 18.3.28 | MIT | web UI (locked) | build and test only |
| @types/react-dom | 18.3.7 | MIT | web UI (locked) | build and test only |
| @typescript-eslint/parser | 7.2.0 | BSD-2-Clause | web UI (locked) | build and test only |
| @typescript-eslint/scope-manager | 7.2.0 | MIT | web UI (locked) | build and test only |
| @typescript-eslint/types | 7.2.0 | MIT | web UI (locked) | build and test only |
| @typescript-eslint/typescript-estree | 7.2.0 | BSD-2-Clause | web UI (locked) | build and test only |
| @typescript-eslint/visitor-keys | 7.2.0 | MIT | web UI (locked) | build and test only |
| @ungap/structured-clone | 1.3.0 | ISC | web UI (locked) | build and test only |
| @unrs/resolver-binding-android-arm-eabi | 1.11.1 | MIT | web UI (locked) | build and test only |
| @unrs/resolver-binding-android-arm64 | 1.11.1 | MIT | web UI (locked) | build and test only |
| @unrs/resolver-binding-darwin-arm64 | 1.11.1 | MIT | web UI (locked) | build and test only |
| @unrs/resolver-binding-darwin-x64 | 1.11.1 | MIT | web UI (locked) | build and test only |
| @unrs/resolver-binding-freebsd-x64 | 1.11.1 | MIT | web UI (locked) | build and test only |
| @unrs/resolver-binding-linux-arm-gnueabihf | 1.11.1 | MIT | web UI (locked) | build and test only |
| @unrs/resolver-binding-linux-arm-musleabihf | 1.11.1 | MIT | web UI (locked) | build and test only |
| @unrs/resolver-binding-linux-arm64-gnu | 1.11.1 | MIT | web UI (locked) | build and test only |
| @unrs/resolver-binding-linux-arm64-musl | 1.11.1 | MIT | web UI (locked) | build and test only |
| @unrs/resolver-binding-linux-ppc64-gnu | 1.11.1 | MIT | web UI (locked) | build and test only |
| @unrs/resolver-binding-linux-riscv64-gnu | 1.11.1 | MIT | web UI (locked) | build and test only |
| @unrs/resolver-binding-linux-riscv64-musl | 1.11.1 | MIT | web UI (locked) | build and test only |
| @unrs/resolver-binding-linux-s390x-gnu | 1.11.1 | MIT | web UI (locked) | build and test only |
| @unrs/resolver-binding-linux-x64-gnu | 1.11.1 | MIT | web UI (locked) | build and test only |
| @unrs/resolver-binding-linux-x64-musl | 1.11.1 | MIT | web UI (locked) | build and test only |
| @unrs/resolver-binding-wasm32-wasi | 1.11.1 | MIT | web UI (locked) | build and test only |
| @unrs/resolver-binding-win32-arm64-msvc | 1.11.1 | MIT | web UI (locked) | build and test only |
| @unrs/resolver-binding-win32-ia32-msvc | 1.11.1 | MIT | web UI (locked) | build and test only |
| @unrs/resolver-binding-win32-x64-msvc | 1.11.1 | MIT | web UI (locked) | build and test only |
| @vitest/expect | 4.1.11 | MIT | web UI (locked) | build and test only |
| @vitest/mocker | 4.1.11 | MIT | web UI (locked) | build and test only |
| @vitest/pretty-format | 4.1.11 | MIT | web UI (locked) | build and test only |
| @vitest/runner | 4.1.11 | MIT | web UI (locked) | build and test only |
| @vitest/snapshot | 4.1.11 | MIT | web UI (locked) | build and test only |
| @vitest/spy | 4.1.11 | MIT | web UI (locked) | build and test only |
| @vitest/utils | 4.1.11 | MIT | web UI (locked) | build and test only |
| acorn | 8.16.0 | MIT | web UI (locked) | build and test only |
| acorn-jsx | 5.3.2 | MIT | web UI (locked) | build and test only |
| agent-base | 7.1.4 | MIT | web UI (locked) | build and test only |
| ajv | 6.14.0 | MIT | web UI (locked) | build and test only |
| ansi-regex | 5.0.1 | MIT | web UI (locked) | build and test only |
| ansi-regex | 6.2.2 | MIT | web UI (locked) | build and test only |
| ansi-styles | 4.3.0 | MIT | web UI (locked) | build and test only |
| ansi-styles | 5.2.0 | MIT | web UI (locked) | build and test only |
| ansi-styles | 6.2.3 | MIT | web UI (locked) | build and test only |
| any-promise | 1.3.0 | MIT | web UI (locked) | build and test only |
| anymatch | 3.1.3 | ISC | web UI (locked) | build and test only |
| arg | 5.0.2 | MIT | web UI (locked) | build and test only |
| argparse | 2.0.1 | Python-2.0 | web UI (locked) | build and test only |
| aria-query | 5.3.0 | Apache-2.0 | web UI (locked) | build and test only |
| aria-query | 5.3.2 | Apache-2.0 | web UI (locked) | build and test only |
| array-buffer-byte-length | 1.0.2 | MIT | web UI (locked) | build and test only |
| array-includes | 3.1.9 | MIT | web UI (locked) | build and test only |
| array-union | 2.1.0 | MIT | web UI (locked) | build and test only |
| array.prototype.findlast | 1.2.5 | MIT | web UI (locked) | build and test only |
| array.prototype.findlastindex | 1.2.6 | MIT | web UI (locked) | build and test only |
| array.prototype.flat | 1.3.3 | MIT | web UI (locked) | build and test only |
| array.prototype.flatmap | 1.3.3 | MIT | web UI (locked) | build and test only |
| array.prototype.tosorted | 1.1.4 | MIT | web UI (locked) | build and test only |
| arraybuffer.prototype.slice | 1.0.4 | MIT | web UI (locked) | build and test only |
| assertion-error | 2.0.1 | MIT | web UI (locked) | build and test only |
| ast-types-flow | 0.0.8 | MIT | web UI (locked) | build and test only |
| async-function | 1.0.0 | MIT | web UI (locked) | build and test only |
| autoprefixer | 10.4.27 | MIT | web UI (locked) | build and test only |
| available-typed-arrays | 1.0.7 | MIT | web UI (locked) | build and test only |
| axe-core | 4.11.2 | MPL-2.0 | web UI (locked) | build and test only |
| axobject-query | 4.1.0 | Apache-2.0 | web UI (locked) | build and test only |
| balanced-match | 1.0.2 | MIT | web UI (locked) | build and test only |
| baseline-browser-mapping | 2.10.18 | Apache-2.0 | web UI (locked) | build and test only |
| binary-extensions | 2.3.0 | MIT | web UI (locked) | build and test only |
| brace-expansion | 1.1.14 | MIT | web UI (locked) | build and test only |
| brace-expansion | 2.1.0 | MIT | web UI (locked) | build and test only |
| braces | 3.0.3 | MIT | web UI (locked) | build and test only |
| browserslist | 4.28.2 | MIT | web UI (locked) | build and test only |
| busboy | 1.6.0 | MIT | web UI (locked) | web image |
| call-bind | 1.0.9 | MIT | web UI (locked) | build and test only |
| call-bind-apply-helpers | 1.0.2 | MIT | web UI (locked) | build and test only |
| call-bound | 1.0.4 | MIT | web UI (locked) | build and test only |
| callsites | 3.1.0 | MIT | web UI (locked) | build and test only |
| camelcase-css | 2.0.1 | MIT | web UI (locked) | build and test only |
| caniuse-lite | 1.0.30001787 | CC-BY-4.0 | web UI (locked) | web image |
| chai | 6.2.2 | MIT | web UI (locked) | build and test only |
| chalk | 4.1.2 | MIT | web UI (locked) | build and test only |
| chokidar | 3.6.0 | MIT | web UI (locked) | build and test only |
| client-only | 0.0.1 | MIT | web UI (locked) | web image |
| color-convert | 2.0.1 | MIT | web UI (locked) | build and test only |
| color-name | 1.1.4 | MIT | web UI (locked) | build and test only |
| commander | 4.1.1 | MIT | web UI (locked) | build and test only |
| concat-map | 0.0.1 | MIT | web UI (locked) | build and test only |
| convert-source-map | 2.0.0 | MIT | web UI (locked) | build and test only |
| cross-spawn | 7.0.6 | MIT | web UI (locked) | build and test only |
| cssesc | 3.0.0 | MIT | web UI (locked) | build and test only |
| cssstyle | 4.6.0 | MIT | web UI (locked) | build and test only |
| csstype | 3.2.3 | MIT | web UI (locked) | build and test only |
| damerau-levenshtein | 1.0.8 | BSD-2-Clause | web UI (locked) | build and test only |
| data-urls | 5.0.0 | MIT | web UI (locked) | build and test only |
| data-view-buffer | 1.0.2 | MIT | web UI (locked) | build and test only |
| data-view-byte-length | 1.0.2 | MIT | web UI (locked) | build and test only |
| data-view-byte-offset | 1.0.1 | MIT | web UI (locked) | build and test only |
| debug | 3.2.7 | MIT | web UI (locked) | build and test only |
| debug | 4.4.3 | MIT | web UI (locked) | build and test only |
| decimal.js | 10.6.0 | MIT | web UI (locked) | build and test only |
| deep-is | 0.1.4 | MIT | web UI (locked) | build and test only |
| define-data-property | 1.1.4 | MIT | web UI (locked) | build and test only |
| define-properties | 1.2.1 | MIT | web UI (locked) | build and test only |
| dequal | 2.0.3 | MIT | web UI (locked) | build and test only |
| detect-libc | 2.1.2 | Apache-2.0 | web UI (locked) | build and test only |
| didyoumean | 1.2.2 | Apache-2.0 | web UI (locked) | build and test only |
| dir-glob | 3.0.1 | MIT | web UI (locked) | build and test only |
| dlv | 1.1.3 | MIT | web UI (locked) | build and test only |
| doctrine | 2.1.0 | Apache-2.0 | web UI (locked) | build and test only |
| doctrine | 3.0.0 | Apache-2.0 | web UI (locked) | build and test only |
| dom-accessibility-api | 0.5.16 | MIT | web UI (locked) | build and test only |
| dunder-proto | 1.0.1 | MIT | web UI (locked) | build and test only |
| eastasianwidth | 0.2.0 | MIT | web UI (locked) | build and test only |
| electron-to-chromium | 1.5.335 | ISC | web UI (locked) | build and test only |
| emoji-regex | 8.0.0 | MIT | web UI (locked) | build and test only |
| emoji-regex | 9.2.2 | MIT | web UI (locked) | build and test only |
| entities | 6.0.1 | BSD-2-Clause | web UI (locked) | build and test only |
| es-abstract | 1.24.2 | MIT | web UI (locked) | build and test only |
| es-define-property | 1.0.1 | MIT | web UI (locked) | build and test only |
| es-errors | 1.3.0 | MIT | web UI (locked) | build and test only |
| es-iterator-helpers | 1.3.2 | MIT | web UI (locked) | build and test only |
| es-module-lexer | 2.3.2 | MIT | web UI (locked) | build and test only |
| es-object-atoms | 1.1.1 | MIT | web UI (locked) | build and test only |
| es-set-tostringtag | 2.1.0 | MIT | web UI (locked) | build and test only |
| es-shim-unscopables | 1.1.0 | MIT | web UI (locked) | build and test only |
| es-to-primitive | 1.3.0 | MIT | web UI (locked) | build and test only |
| escalade | 3.2.0 | MIT | web UI (locked) | build and test only |
| escape-string-regexp | 4.0.0 | MIT | web UI (locked) | build and test only |
| eslint | 8.57.1 | MIT | web UI (locked) | build and test only |
| eslint-config-next | 14.2.5 | MIT | web UI (locked) | build and test only |
| eslint-import-resolver-node | 0.3.10 | MIT | web UI (locked) | build and test only |
| eslint-import-resolver-typescript | 3.10.1 | ISC | web UI (locked) | build and test only |
| eslint-module-utils | 2.12.1 | MIT | web UI (locked) | build and test only |
| eslint-plugin-import | 2.32.0 | MIT | web UI (locked) | build and test only |
| eslint-plugin-jsx-a11y | 6.10.2 | MIT | web UI (locked) | build and test only |
| eslint-plugin-react | 7.37.5 | MIT | web UI (locked) | build and test only |
| eslint-plugin-react-hooks | 5.0.0-canary-7118f5dd7-20230705 | MIT | web UI (locked) | build and test only |
| eslint-scope | 7.2.2 | BSD-2-Clause | web UI (locked) | build and test only |
| eslint-visitor-keys | 3.4.3 | Apache-2.0 | web UI (locked) | build and test only |
| espree | 9.6.1 | BSD-2-Clause | web UI (locked) | build and test only |
| esquery | 1.7.0 | BSD-3-Clause | web UI (locked) | build and test only |
| esrecurse | 4.3.0 | BSD-2-Clause | web UI (locked) | build and test only |
| estraverse | 5.3.0 | BSD-2-Clause | web UI (locked) | build and test only |
| estree-walker | 3.0.3 | MIT | web UI (locked) | build and test only |
| esutils | 2.0.3 | BSD-2-Clause | web UI (locked) | build and test only |
| expect-type | 1.4.0 | Apache-2.0 | web UI (locked) | build and test only |
| fast-deep-equal | 3.1.3 | MIT | web UI (locked) | build and test only |
| fast-glob | 3.3.3 | MIT | web UI (locked) | build and test only |
| fast-json-stable-stringify | 2.1.0 | MIT | web UI (locked) | build and test only |
| fast-levenshtein | 2.0.6 | MIT | web UI (locked) | build and test only |
| fastq | 1.20.1 | ISC | web UI (locked) | build and test only |
| fdir | 6.5.0 | MIT | web UI (locked) | build and test only |
| file-entry-cache | 6.0.1 | MIT | web UI (locked) | build and test only |
| fill-range | 7.1.1 | MIT | web UI (locked) | build and test only |
| find-up | 5.0.0 | MIT | web UI (locked) | build and test only |
| flat-cache | 3.2.0 | MIT | web UI (locked) | build and test only |
| flatted | 3.4.2 | ISC | web UI (locked) | build and test only |
| for-each | 0.3.5 | MIT | web UI (locked) | build and test only |
| foreground-child | 3.3.1 | ISC | web UI (locked) | build and test only |
| fraction.js | 5.3.4 | MIT | web UI (locked) | build and test only |
| fs.realpath | 1.0.0 | ISC | web UI (locked) | build and test only |
| fsevents | 2.3.2 | MIT | web UI (locked) | build and test only |
| fsevents | 2.3.3 | MIT | web UI (locked) | build and test only |
| function-bind | 1.1.2 | MIT | web UI (locked) | build and test only |
| function.prototype.name | 1.1.8 | MIT | web UI (locked) | build and test only |
| functions-have-names | 1.2.3 | MIT | web UI (locked) | build and test only |
| generator-function | 2.0.1 | MIT | web UI (locked) | build and test only |
| get-intrinsic | 1.3.0 | MIT | web UI (locked) | build and test only |
| get-proto | 1.0.1 | MIT | web UI (locked) | build and test only |
| get-symbol-description | 1.1.0 | MIT | web UI (locked) | build and test only |
| get-tsconfig | 4.13.7 | MIT | web UI (locked) | build and test only |
| glob | 10.3.10 | ISC | web UI (locked) | build and test only |
| glob | 7.2.3 | ISC | web UI (locked) | build and test only |
| glob-parent | 5.1.2 | ISC | web UI (locked) | build and test only |
| glob-parent | 6.0.2 | ISC | web UI (locked) | build and test only |
| globals | 13.24.0 | MIT | web UI (locked) | build and test only |
| globalthis | 1.0.4 | MIT | web UI (locked) | build and test only |
| globby | 11.1.0 | MIT | web UI (locked) | build and test only |
| gopd | 1.2.0 | MIT | web UI (locked) | build and test only |
| graceful-fs | 4.2.11 | ISC | web UI (locked) | web image |
| graphemer | 1.4.0 | MIT | web UI (locked) | build and test only |
| has-bigints | 1.1.0 | MIT | web UI (locked) | build and test only |
| has-flag | 4.0.0 | MIT | web UI (locked) | build and test only |
| has-property-descriptors | 1.0.2 | MIT | web UI (locked) | build and test only |
| has-proto | 1.2.0 | MIT | web UI (locked) | build and test only |
| has-symbols | 1.1.0 | MIT | web UI (locked) | build and test only |
| has-tostringtag | 1.0.2 | MIT | web UI (locked) | build and test only |
| hasown | 2.0.2 | MIT | web UI (locked) | build and test only |
| html-encoding-sniffer | 4.0.0 | MIT | web UI (locked) | build and test only |
| http-proxy-agent | 7.0.2 | MIT | web UI (locked) | build and test only |
| https-proxy-agent | 7.0.6 | MIT | web UI (locked) | build and test only |
| iconv-lite | 0.6.3 | MIT | web UI (locked) | build and test only |
| ignore | 5.3.2 | MIT | web UI (locked) | build and test only |
| import-fresh | 3.3.1 | MIT | web UI (locked) | build and test only |
| imurmurhash | 0.1.4 | MIT | web UI (locked) | build and test only |
| inflight | 1.0.6 | ISC | web UI (locked) | build and test only |
| inherits | 2.0.4 | ISC | web UI (locked) | build and test only |
| internal-slot | 1.1.0 | MIT | web UI (locked) | build and test only |
| is-array-buffer | 3.0.5 | MIT | web UI (locked) | build and test only |
| is-async-function | 2.1.1 | MIT | web UI (locked) | build and test only |
| is-bigint | 1.1.0 | MIT | web UI (locked) | build and test only |
| is-binary-path | 2.1.0 | MIT | web UI (locked) | build and test only |
| is-boolean-object | 1.2.2 | MIT | web UI (locked) | build and test only |
| is-bun-module | 2.0.0 | MIT | web UI (locked) | build and test only |
| is-callable | 1.2.7 | MIT | web UI (locked) | build and test only |
| is-core-module | 2.16.1 | MIT | web UI (locked) | build and test only |
| is-data-view | 1.0.2 | MIT | web UI (locked) | build and test only |
| is-date-object | 1.1.0 | MIT | web UI (locked) | build and test only |
| is-extglob | 2.1.1 | MIT | web UI (locked) | build and test only |
| is-finalizationregistry | 1.1.1 | MIT | web UI (locked) | build and test only |
| is-fullwidth-code-point | 3.0.0 | MIT | web UI (locked) | build and test only |
| is-generator-function | 1.1.2 | MIT | web UI (locked) | build and test only |
| is-glob | 4.0.3 | MIT | web UI (locked) | build and test only |
| is-map | 2.0.3 | MIT | web UI (locked) | build and test only |
| is-negative-zero | 2.0.3 | MIT | web UI (locked) | build and test only |
| is-number | 7.0.0 | MIT | web UI (locked) | build and test only |
| is-number-object | 1.1.1 | MIT | web UI (locked) | build and test only |
| is-path-inside | 3.0.3 | MIT | web UI (locked) | build and test only |
| is-potential-custom-element-name | 1.0.1 | MIT | web UI (locked) | build and test only |
| is-regex | 1.2.1 | MIT | web UI (locked) | build and test only |
| is-set | 2.0.3 | MIT | web UI (locked) | build and test only |
| is-shared-array-buffer | 1.0.4 | MIT | web UI (locked) | build and test only |
| is-string | 1.1.1 | MIT | web UI (locked) | build and test only |
| is-symbol | 1.1.1 | MIT | web UI (locked) | build and test only |
| is-typed-array | 1.1.15 | MIT | web UI (locked) | build and test only |
| is-weakmap | 2.0.2 | MIT | web UI (locked) | build and test only |
| is-weakref | 1.1.1 | MIT | web UI (locked) | build and test only |
| is-weakset | 2.0.4 | MIT | web UI (locked) | build and test only |
| isarray | 2.0.5 | MIT | web UI (locked) | build and test only |
| isexe | 2.0.0 | ISC | web UI (locked) | build and test only |
| iterator.prototype | 1.1.5 | MIT | web UI (locked) | build and test only |
| jackspeak | 2.3.6 | BlueOak-1.0.0 | web UI (locked) | build and test only |
| jiti | 1.21.7 | MIT | web UI (locked) | build and test only |
| js-tokens | 4.0.0 | MIT | web UI (locked) | build and test only |
| js-yaml | 4.1.1 | MIT | web UI (locked) | build and test only |
| jsdom | 26.1.0 | MIT | web UI (locked) | build and test only |
| json-buffer | 3.0.1 | MIT | web UI (locked) | build and test only |
| json-schema-traverse | 0.4.1 | MIT | web UI (locked) | build and test only |
| json-stable-stringify-without-jsonify | 1.0.1 | MIT | web UI (locked) | build and test only |
| json5 | 1.0.2 | MIT | web UI (locked) | build and test only |
| jsx-ast-utils | 3.3.5 | MIT | web UI (locked) | build and test only |
| keyv | 4.5.4 | MIT | web UI (locked) | build and test only |
| language-subtag-registry | 0.3.23 | CC0-1.0 | web UI (locked) | build and test only |
| language-tags | 1.0.9 | MIT | web UI (locked) | build and test only |
| levn | 0.4.1 | MIT | web UI (locked) | build and test only |
| lightningcss | 1.33.0 | MPL-2.0 | web UI (locked) | build and test only |
| lightningcss-android-arm64 | 1.33.0 | MPL-2.0 | web UI (locked) | build and test only |
| lightningcss-darwin-arm64 | 1.33.0 | MPL-2.0 | web UI (locked) | build and test only |
| lightningcss-darwin-x64 | 1.33.0 | MPL-2.0 | web UI (locked) | build and test only |
| lightningcss-freebsd-x64 | 1.33.0 | MPL-2.0 | web UI (locked) | build and test only |
| lightningcss-linux-arm-gnueabihf | 1.33.0 | MPL-2.0 | web UI (locked) | build and test only |
| lightningcss-linux-arm64-gnu | 1.33.0 | MPL-2.0 | web UI (locked) | build and test only |
| lightningcss-linux-arm64-musl | 1.33.0 | MPL-2.0 | web UI (locked) | build and test only |
| lightningcss-linux-x64-gnu | 1.33.0 | MPL-2.0 | web UI (locked) | build and test only |
| lightningcss-linux-x64-musl | 1.33.0 | MPL-2.0 | web UI (locked) | build and test only |
| lightningcss-win32-arm64-msvc | 1.33.0 | MPL-2.0 | web UI (locked) | build and test only |
| lightningcss-win32-x64-msvc | 1.33.0 | MPL-2.0 | web UI (locked) | build and test only |
| lilconfig | 3.1.3 | MIT | web UI (locked) | build and test only |
| lines-and-columns | 1.2.4 | MIT | web UI (locked) | build and test only |
| locate-path | 6.0.0 | MIT | web UI (locked) | build and test only |
| lodash.merge | 4.6.2 | MIT | web UI (locked) | build and test only |
| loose-envify | 1.4.0 | MIT | web UI (locked) | build and test only |
| lru-cache | 10.4.3 | ISC | web UI (locked) | build and test only |
| lz-string | 1.5.0 | MIT | web UI (locked) | build and test only |
| magic-string | 0.30.21 | MIT | web UI (locked) | build and test only |
| math-intrinsics | 1.1.0 | MIT | web UI (locked) | build and test only |
| merge2 | 1.4.1 | MIT | web UI (locked) | build and test only |
| micromatch | 4.0.8 | MIT | web UI (locked) | build and test only |
| minimatch | 3.1.5 | ISC | web UI (locked) | build and test only |
| minimatch | 9.0.3 | ISC | web UI (locked) | build and test only |
| minimatch | 9.0.9 | ISC | web UI (locked) | build and test only |
| minimist | 1.2.8 | MIT | web UI (locked) | build and test only |
| minipass | 7.1.3 | BlueOak-1.0.0 | web UI (locked) | build and test only |
| ms | 2.1.3 | MIT | web UI (locked) | build and test only |
| mz | 2.7.0 | MIT | web UI (locked) | build and test only |
| nanoid | 3.3.18 | MIT | web UI (locked) | web image |
| napi-postinstall | 0.3.4 | MIT | web UI (locked) | build and test only |
| natural-compare | 1.4.0 | MIT | web UI (locked) | build and test only |
| next | 14.2.5 | MIT | web UI (locked) | web image |
| node-exports-info | 1.6.0 | MIT | web UI (locked) | build and test only |
| node-releases | 2.0.37 | MIT | web UI (locked) | build and test only |
| normalize-path | 3.0.0 | MIT | web UI (locked) | build and test only |
| nwsapi | 2.2.24 | MIT | web UI (locked) | build and test only |
| object-assign | 4.1.1 | MIT | web UI (locked) | build and test only |
| object-hash | 3.0.0 | MIT | web UI (locked) | build and test only |
| object-inspect | 1.13.4 | MIT | web UI (locked) | build and test only |
| object-keys | 1.1.1 | MIT | web UI (locked) | build and test only |
| object.assign | 4.1.7 | MIT | web UI (locked) | build and test only |
| object.entries | 1.1.9 | MIT | web UI (locked) | build and test only |
| object.fromentries | 2.0.8 | MIT | web UI (locked) | build and test only |
| object.groupby | 1.0.3 | MIT | web UI (locked) | build and test only |
| object.values | 1.2.1 | MIT | web UI (locked) | build and test only |
| obug | 2.1.4 | MIT | web UI (locked) | build and test only |
| once | 1.4.0 | ISC | web UI (locked) | build and test only |
| optionator | 0.9.4 | MIT | web UI (locked) | build and test only |
| own-keys | 1.0.1 | MIT | web UI (locked) | build and test only |
| p-limit | 3.1.0 | MIT | web UI (locked) | build and test only |
| p-locate | 5.0.0 | MIT | web UI (locked) | build and test only |
| parent-module | 1.0.1 | MIT | web UI (locked) | build and test only |
| parse5 | 7.3.0 | MIT | web UI (locked) | build and test only |
| path-exists | 4.0.0 | MIT | web UI (locked) | build and test only |
| path-is-absolute | 1.0.1 | MIT | web UI (locked) | build and test only |
| path-key | 3.1.1 | MIT | web UI (locked) | build and test only |
| path-parse | 1.0.7 | MIT | web UI (locked) | build and test only |
| path-scurry | 1.11.1 | BlueOak-1.0.0 | web UI (locked) | build and test only |
| path-type | 4.0.0 | MIT | web UI (locked) | build and test only |
| pathe | 2.0.3 | MIT | web UI (locked) | build and test only |
| picocolors | 1.1.1 | ISC | web UI (locked) | web image |
| picomatch | 2.3.2 | MIT | web UI (locked) | build and test only |
| picomatch | 4.0.4 | MIT | web UI (locked) | build and test only |
| picomatch | 4.0.5 | MIT | web UI (locked) | build and test only |
| pify | 2.3.0 | MIT | web UI (locked) | build and test only |
| pirates | 4.0.7 | MIT | web UI (locked) | build and test only |
| playwright | 1.56.1 | Apache-2.0 | web UI (locked) | build and test only |
| playwright-core | 1.56.1 | Apache-2.0 | web UI (locked) | build and test only |
| possible-typed-array-names | 1.1.0 | MIT | web UI (locked) | build and test only |
| postcss | 8.4.31 | MIT | web UI (locked) | web image |
| postcss | 8.5.26 | MIT | web UI (locked) | build and test only |
| postcss-import | 15.1.0 | MIT | web UI (locked) | build and test only |
| postcss-js | 4.1.0 | MIT | web UI (locked) | build and test only |
| postcss-load-config | 6.0.1 | MIT | web UI (locked) | build and test only |
| postcss-nested | 6.2.0 | MIT | web UI (locked) | build and test only |
| postcss-selector-parser | 6.1.2 | MIT | web UI (locked) | build and test only |
| postcss-value-parser | 4.2.0 | MIT | web UI (locked) | build and test only |
| prelude-ls | 1.2.1 | MIT | web UI (locked) | build and test only |
| pretty-format | 27.5.1 | MIT | web UI (locked) | build and test only |
| prop-types | 15.8.1 | MIT | web UI (locked) | build and test only |
| punycode | 2.3.1 | MIT | web UI (locked) | build and test only |
| queue-microtask | 1.2.3 | MIT | web UI (locked) | build and test only |
| react | 18.3.1 | MIT | web UI (locked) | web image |
| react-dom | 18.3.1 | MIT | web UI (locked) | web image |
| react-is | 16.13.1 | MIT | web UI (locked) | build and test only |
| react-is | 17.0.2 | MIT | web UI (locked) | build and test only |
| read-cache | 1.0.0 | MIT | web UI (locked) | build and test only |
| readdirp | 3.6.0 | MIT | web UI (locked) | build and test only |
| reflect.getprototypeof | 1.0.10 | MIT | web UI (locked) | build and test only |
| regexp.prototype.flags | 1.5.4 | MIT | web UI (locked) | build and test only |
| resolve | 1.22.12 | MIT | web UI (locked) | build and test only |
| resolve | 2.0.0-next.6 | MIT | web UI (locked) | build and test only |
| resolve-from | 4.0.0 | MIT | web UI (locked) | build and test only |
| resolve-pkg-maps | 1.0.0 | MIT | web UI (locked) | build and test only |
| reusify | 1.1.0 | MIT | web UI (locked) | build and test only |
| rimraf | 3.0.2 | ISC | web UI (locked) | build and test only |
| rolldown | 1.2.5 | MIT | web UI (locked) | build and test only |
| rrweb-cssom | 0.8.0 | MIT | web UI (locked) | build and test only |
| run-parallel | 1.2.0 | MIT | web UI (locked) | build and test only |
| safe-array-concat | 1.1.3 | MIT | web UI (locked) | build and test only |
| safe-push-apply | 1.0.0 | MIT | web UI (locked) | build and test only |
| safe-regex-test | 1.1.0 | MIT | web UI (locked) | build and test only |
| safer-buffer | 2.1.2 | MIT | web UI (locked) | build and test only |
| saxes | 6.0.0 | ISC | web UI (locked) | build and test only |
| scheduler | 0.23.2 | MIT | web UI (locked) | web image |
| semver | 6.3.1 | ISC | web UI (locked) | build and test only |
| semver | 7.7.4 | ISC | web UI (locked) | build and test only |
| set-function-length | 1.2.2 | MIT | web UI (locked) | build and test only |
| set-function-name | 2.0.2 | MIT | web UI (locked) | build and test only |
| set-proto | 1.0.0 | MIT | web UI (locked) | build and test only |
| shebang-command | 2.0.0 | MIT | web UI (locked) | build and test only |
| shebang-regex | 3.0.0 | MIT | web UI (locked) | build and test only |
| side-channel | 1.1.0 | MIT | web UI (locked) | build and test only |
| side-channel-list | 1.0.1 | MIT | web UI (locked) | build and test only |
| side-channel-map | 1.0.1 | MIT | web UI (locked) | build and test only |
| side-channel-weakmap | 1.0.2 | MIT | web UI (locked) | build and test only |
| siginfo | 2.0.0 | ISC | web UI (locked) | build and test only |
| signal-exit | 4.1.0 | ISC | web UI (locked) | build and test only |
| slash | 3.0.0 | MIT | web UI (locked) | build and test only |
| source-map-js | 1.2.1 | BSD-3-Clause | web UI (locked) | web image |
| stable-hash | 0.0.5 | MIT | web UI (locked) | build and test only |
| stackback | 0.0.2 | MIT | web UI (locked) | build and test only |
| std-env | 4.2.0 | MIT | web UI (locked) | build and test only |
| stop-iteration-iterator | 1.1.0 | MIT | web UI (locked) | build and test only |
| streamsearch | 1.1.0 | MIT | web UI (locked) | web image |
| string-width | 4.2.3 | MIT | web UI (locked) | build and test only |
| string-width | 5.1.2 | MIT | web UI (locked) | build and test only |
| string.prototype.includes | 2.0.1 | MIT | web UI (locked) | build and test only |
| string.prototype.matchall | 4.0.12 | MIT | web UI (locked) | build and test only |
| string.prototype.repeat | 1.0.0 | MIT | web UI (locked) | build and test only |
| string.prototype.trim | 1.2.10 | MIT | web UI (locked) | build and test only |
| string.prototype.trimend | 1.0.9 | MIT | web UI (locked) | build and test only |
| string.prototype.trimstart | 1.0.8 | MIT | web UI (locked) | build and test only |
| strip-ansi | 6.0.1 | MIT | web UI (locked) | build and test only |
| strip-ansi | 7.2.0 | MIT | web UI (locked) | build and test only |
| strip-bom | 3.0.0 | MIT | web UI (locked) | build and test only |
| strip-json-comments | 3.1.1 | MIT | web UI (locked) | build and test only |
| styled-jsx | 5.1.1 | MIT | web UI (locked) | web image |
| sucrase | 3.35.1 | MIT | web UI (locked) | build and test only |
| supports-color | 7.2.0 | MIT | web UI (locked) | build and test only |
| supports-preserve-symlinks-flag | 1.0.0 | MIT | web UI (locked) | build and test only |
| symbol-tree | 3.2.4 | MIT | web UI (locked) | build and test only |
| tailwindcss | 3.4.19 | MIT | web UI (locked) | web image |
| text-table | 0.2.0 | MIT | web UI (locked) | build and test only |
| thenify | 3.3.1 | MIT | web UI (locked) | build and test only |
| thenify-all | 1.6.0 | MIT | web UI (locked) | build and test only |
| tinybench | 2.9.0 | MIT | web UI (locked) | build and test only |
| tinyexec | 1.3.0 | MIT | web UI (locked) | build and test only |
| tinyglobby | 0.2.17 | MIT | web UI (locked) | build and test only |
| tinyrainbow | 3.1.1 | MIT | web UI (locked) | build and test only |
| tldts | 6.1.86 | MIT | web UI (locked) | build and test only |
| tldts-core | 6.1.86 | MIT | web UI (locked) | build and test only |
| to-regex-range | 5.0.1 | MIT | web UI (locked) | build and test only |
| tough-cookie | 5.1.2 | BSD-3-Clause | web UI (locked) | build and test only |
| tr46 | 5.1.1 | MIT | web UI (locked) | build and test only |
| ts-api-utils | 1.4.3 | MIT | web UI (locked) | build and test only |
| ts-interface-checker | 0.1.13 | Apache-2.0 | web UI (locked) | build and test only |
| tsconfig-paths | 3.15.0 | MIT | web UI (locked) | build and test only |
| tslib | 2.8.1 | 0BSD | web UI (locked) | build and test only |
| type-check | 0.4.0 | MIT | web UI (locked) | build and test only |
| type-fest | 0.20.2 | (MIT OR CC0-1.0) | web UI (locked) | build and test only |
| typed-array-buffer | 1.0.3 | MIT | web UI (locked) | build and test only |
| typed-array-byte-length | 1.0.3 | MIT | web UI (locked) | build and test only |
| typed-array-byte-offset | 1.0.4 | MIT | web UI (locked) | build and test only |
| typed-array-length | 1.0.7 | MIT | web UI (locked) | build and test only |
| typescript | 5.9.3 | Apache-2.0 | web UI (locked) | build and test only |
| unbox-primitive | 1.1.0 | MIT | web UI (locked) | build and test only |
| undici-types | 6.21.0 | MIT | web UI (locked) | build and test only |
| unrs-resolver | 1.11.1 | MIT | web UI (locked) | build and test only |
| update-browserslist-db | 1.2.3 | MIT | web UI (locked) | build and test only |
| uri-js | 4.4.1 | BSD-2-Clause | web UI (locked) | build and test only |
| util-deprecate | 1.0.2 | MIT | web UI (locked) | build and test only |
| vite | 8.2.2 | MIT | web UI (locked) | build and test only |
| vitest | 4.1.11 | MIT | web UI (locked) | build and test only |
| w3c-xmlserializer | 5.0.0 | MIT | web UI (locked) | build and test only |
| web-vitals | 6.1.1 | Apache-2.0 | web UI (locked) | web image |
| webidl-conversions | 7.0.0 | BSD-2-Clause | web UI (locked) | build and test only |
| whatwg-encoding | 3.1.1 | MIT | web UI (locked) | build and test only |
| whatwg-mimetype | 4.0.0 | MIT | web UI (locked) | build and test only |
| whatwg-url | 14.2.0 | MIT | web UI (locked) | build and test only |
| which | 2.0.2 | ISC | web UI (locked) | build and test only |
| which-boxed-primitive | 1.1.1 | MIT | web UI (locked) | build and test only |
| which-builtin-type | 1.2.1 | MIT | web UI (locked) | build and test only |
| which-collection | 1.0.2 | MIT | web UI (locked) | build and test only |
| which-typed-array | 1.1.20 | MIT | web UI (locked) | build and test only |
| why-is-node-running | 2.3.0 | MIT | web UI (locked) | build and test only |
| word-wrap | 1.2.5 | MIT | web UI (locked) | build and test only |
| wrap-ansi | 7.0.0 | MIT | web UI (locked) | build and test only |
| wrap-ansi | 8.1.0 | MIT | web UI (locked) | build and test only |
| wrappy | 1.0.2 | ISC | web UI (locked) | build and test only |
| ws | 8.21.3 | MIT | web UI (locked) | build and test only |
| xml-name-validator | 5.0.0 | Apache-2.0 | web UI (locked) | build and test only |
| xmlchars | 2.2.0 | MIT | web UI (locked) | build and test only |
| yocto-queue | 0.1.0 | MIT | web UI (locked) | build and test only |

## Container images

The images the compose files run and the Dockerfiles build from. The
recipient pulls each one; the project pushes none. Each is pinned by its
tag and by the digest of the multi-arch index that tag pointed to when it
was last reviewed (R14), so a pull cannot change under the tree, and the
version in the notes is that digest's. `scripts/refresh_image_digests.py`
moves a digest in a reviewed pull request: it resolves each from two
agreeing sources and rewrites the reference and its Digest cell here
together. `scripts/check_dependency_identity.py` fails on a reference that
has no digest.

| Image | Tag | Digest | Licence | Pulled by | Notes |
|---|---|---|---|---|---|
| docker.io/library/postgres | 16-alpine | sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea | PostgreSQL | compose, the `postgres` service | PostgreSQL 16.15 on Alpine 3.24; Alpine packages under their own licences. Resolved on 2026-09-30 from the registry, Docker Hub's tags API and mirror.gcr.io, which agreed. |
| docker.io/valkey/valkey | 8-alpine | sha256:081c2f5cb575efc901aa80ff9cdbd1ec6a301682fd35e1ebb4b0990a4a4a8507 | BSD-3-Clause | compose, the `redis` service (L38) | Valkey 8.1.10 on Alpine 3.24, the Valkey project's own image (its source label names the project's repository); Alpine packages under their own licences. Resolved on 2026-09-30 from the registry, Docker Hub's tags API and mirror.gcr.io, which agreed. |
| docker.io/timberio/vector | 0.50.0-alpine | sha256:93761c26fa3a3793f5f200de0f4cfc6102b5e9803ab33b4830d0873ba8dbdc4f | MPL-2.0 | compose, the `vector` service | Resolved on 2026-09-30 from the registry, Docker Hub's tags API and mirror.gcr.io, which agreed. |
| docker.io/jaegertracing/all-in-one | 1.66.0 | sha256:9864182b4e01350fcc64631bdba5f4085f87daae9d477a04c25d9cb362e787a9 | Apache-2.0 | compose, profiles `viewer` and `full` | Resolved on 2026-09-30 from the registry, Docker Hub's tags API and mirror.gcr.io, which agreed. |
| docker.io/otel/opentelemetry-collector | 0.159.0 | sha256:7725a7a10c87d8853208bdd4bb3439ad3c0d7b32b4292b9300ac07c8daba14a2 | Apache-2.0 | compose, profiles `obs` and `cribl` | Resolved on 2026-09-30 from the registry, Docker Hub's tags API and mirror.gcr.io, which agreed. |
| docker.io/library/caddy | 2.11.4 | sha256:0c994536bddb66445885237f1a5dcc1916bccea922661c76b4e9fc24061f9b52 | Apache-2.0 | compose, the `edge` service (profile `tls`) | Caddy 2.11.4 on Alpine 3.23.6; Alpine packages under their own licences. Resolved on 2026-09-28 from the registry, Docker Hub's tags API and mirror.gcr.io, which agreed. |
| docker.io/library/python | 3.12-slim | sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f | PSF-2.0 | the backend and gateway builds, and the Python example and template builds | CPython 3.12.14 on Debian; Debian packages under their own licences (their copyright files are in the image). Resolved on 2026-09-30 from the registry, Docker Hub's tags API and mirror.gcr.io, which agreed. |
| docker.io/library/node | 22-slim | sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c | MIT | the web UI build, and the TypeScript example and template builds | Node.js 22.23.3 (it bundles OpenSSL, ICU, V8 and others under their licences), npm (Artistic-2.0) and Yarn 1.22.22 (BSD-2-Clause) on Debian. Resolved on 2026-09-30 from the registry, Docker Hub's tags API and mirror.gcr.io, which agreed. |

## Programs LibreRun runs but does not include

Invoked as separate programs, never linked or redistributed: Docker or
Podman and their compose tools, git, `sops` and `age` (the encrypted
secrets store), `uvicorn` and `alembic` (as packages above), Node.js and
npm, `psql` and `pg_isready`, and `valkey-cli`, BusyBox `wget` and
`curl` in health checks inside the upstream images.

## Continuous integration

The GitHub Actions the workflows use (`actions/*`, MIT; the image and
PyPI publishing actions went with the publishing jobs, A2) run on
GitHub's runners and are never part of what LibreRun distributes; nor
are the images CI alone pulls, each pinned by tag and digest like those
above: `docker.io/library/postgres:16`, `docker.io/curlimages/curl:8.10.1`
(MIT), and `docker.io/library/redis:7-alpine`, which `dependency-identity`
runs only to show that Valkey refuses a volume Redis 7.4 wrote (C-18).
Nor is gitleaks (MIT), the tree review's secret scanner:
`scripts/review_public_tree.sh --install` fetches its release tarball from
GitHub and refuses it unless its SHA-256 is the one the script pins
(`docs/release/Distribution_Surface_Matrix.md`).

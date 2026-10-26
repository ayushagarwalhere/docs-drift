"""Import-name to distribution-name knowledge for Python and Node; static tables, no installed metadata."""

from __future__ import annotations

import sys

from docs_drift.deps.manifests import python_key

# Distributions whose import name differs from the PEP 503 name. Values are normalized distribution keys.
IMPORT_TO_DISTRIBUTIONS: dict[str, tuple[str, ...]] = {
    "PIL": ("pillow",),
    "yaml": ("pyyaml",),
    "bs4": ("beautifulsoup4",),
    "sklearn": ("scikit-learn",),
    "skimage": ("scikit-image",),
    "cv2": ("opencv-python", "opencv-python-headless", "opencv-contrib-python", "opencv-contrib-python-headless"),
    "dateutil": ("python-dateutil",),
    "dotenv": ("python-dotenv",),
    "jwt": ("pyjwt",),
    "jose": ("python-jose",),
    "magic": ("python-magic",),
    "multipart": ("python-multipart",),
    "slugify": ("python-slugify",),
    "Crypto": ("pycryptodome", "pycrypto"),
    "Cryptodome": ("pycryptodomex",),
    "OpenSSL": ("pyopenssl",),
    "attr": ("attrs",),
    "serial": ("pyserial",),
    "usb": ("pyusb",),
    "git": ("gitpython",),
    "github": ("pygithub",),
    "gitlab": ("python-gitlab",),
    "docx": ("python-docx",),
    "pptx": ("python-pptx",),
    "fitz": ("pymupdf",),
    "Levenshtein": ("python-levenshtein", "levenshtein"),
    "MySQLdb": ("mysqlclient",),
    "psycopg2": ("psycopg2", "psycopg2-binary"),
    "psycopg": ("psycopg", "psycopg-binary"),
    "pkg_resources": ("setuptools",),
    "_pytest": ("pytest",),
    "websocket": ("websocket-client",),
    "socks": ("pysocks",),
    "telegram": ("python-telegram-bot",),
    "discord": ("discord-py",),
    "markdown_it": ("markdown-it-py",),
    "mdit_py_plugins": ("mdit-py-plugins",),
    "win32api": ("pywin32",),
    "win32con": ("pywin32",),
    "win32com": ("pywin32",),
    "pywintypes": ("pywin32",),
    "pythoncom": ("pywin32",),
    "Xlib": ("python-xlib",),
    "wx": ("wxpython",),
    "gi": ("pygobject",),
    "cairo": ("pycairo",),
    "zmq": ("pyzmq",),
    "nacl": ("pynacl",),
    "ldap": ("python-ldap",),
    "kafka": ("kafka-python",),
    "snappy": ("python-snappy",),
    "faiss": ("faiss-cpu", "faiss-gpu"),
    "umap": ("umap-learn",),
    "Bio": ("biopython",),
    "rest_framework": ("djangorestframework",),
    "corsheaders": ("django-cors-headers",),
    "debug_toolbar": ("django-debug-toolbar",),
    "environ": ("django-environ",),
    "grpc": ("grpcio",),
    "OpenGL": ("pyopengl",),
    "Cython": ("cython",),
    "pyximport": ("cython",),
    "dns": ("dnspython",),
    "memcache": ("python-memcached",),
    "mpl_toolkits": ("matplotlib",),
    "pylab": ("matplotlib",),
    "IPython": ("ipython",),
    "lsprotocol": ("lsprotocol",),
    "pygls": ("pygls",),
    "tomli_w": ("tomli-w",),
    "typing_extensions": ("typing-extensions",),
    "google.protobuf": ("protobuf",),
    "sentry_sdk": ("sentry-sdk",),
    "ruamel": ("ruamel-yaml",),
    "jinja2": ("jinja2",),
    "markupsafe": ("markupsafe",),
    "igraph": ("python-igraph", "igraph"),
    "Image": ("pillow",),
    "uvloop": ("uvloop",),
    "speech_recognition": ("speechrecognition",),
    "googleapiclient": ("google-api-python-client",),
    "playwright": ("playwright",),
    "pydantic_settings": ("pydantic-settings",),
    "httpx": ("httpx",),
    "opentelemetry": ("opentelemetry-api",),
    "opentelemetry.sdk": ("opentelemetry-sdk",),
}
# Packages installed by a declared parent and often imported directly; undeclared use is a weaker signal.
COMPANIONS: dict[str, tuple[str, ...]] = {
    "pydantic-core": ("pydantic",),
    "starlette": ("fastapi",),
    "werkzeug": ("flask",),
    "jinja2": ("flask",),
    "markupsafe": ("jinja2", "flask"),
    "click": ("flask", "typer"),
    "anyio": ("httpx", "starlette", "fastapi"),
    "urllib3": ("requests",),
    "idna": ("requests", "httpx"),
    "certifi": ("requests", "httpx"),
    "httpcore": ("httpx",),
    "typing-extensions": ("pydantic", "typer", "fastapi"),
}
# Namespace roots whose distributions are named after the first dotted components.
NAMESPACE_ROOTS = {"google", "azure", "zope", "jaraco", "sphinxcontrib", "backports", "opentelemetry", "oslo", "ruamel"}
# Removed from newer interpreters but still standard library for older supported ones.
_REMOVED_STDLIB = {
    "aifc",
    "asynchat",
    "asyncore",
    "audioop",
    "cgi",
    "cgitb",
    "chunk",
    "crypt",
    "distutils",
    "imghdr",
    "imp",
    "lib2to3",
    "mailcap",
    "msilib",
    "nis",
    "nntplib",
    "ossaudiodev",
    "pipes",
    "smtpd",
    "sndhdr",
    "spwd",
    "sunau",
    "telnetlib",
    "uu",
    "xdrlib",
    "tomllib",
    "__future__",
}
# Type-checker-only modules that never exist at runtime.
_TYPING_ONLY = {"_typeshed"}
STDLIB = frozenset(sys.stdlib_module_names) | _REMOVED_STDLIB | _TYPING_ONLY
# Distributions that are normally used without an import: tools, plugins, servers, stubs and build helpers.
NON_IMPORTED_PYTHON = {
    "pip",
    "setuptools",
    "wheel",
    "build",
    "twine",
    "uvicorn",
    "gunicorn",
    "hypercorn",
    "daphne",
    "waitress",
    "tzdata",
    "certifi",
    "pre-commit",
    "tox",
    "nox",
    "ruff",
    "black",
    "isort",
    "flake8",
    "pylint",
    "mypy",
    "coverage",
    "pytest-cov",
    "pytest-xdist",
    "pytest-asyncio",
    "pytest-mock",
    "pytest-timeout",
    "mkdocs",
    "mkdocs-material",
    "sphinx",
    "sphinx-rtd-theme",
    "furo",
    "hatchling",
    "hatch-vcs",
    "setuptools-scm",
    "poetry-core",
    "flit-core",
    "pdm-backend",
    "maturin",
    "cython",
    "psycopg2-binary",
    "psycopg-binary",
    "gevent",
    "eventlet",
    "watchdog",
    "python-dotenv",
    "rust-just",
    "pip-audit",
    "deptry",
    "honcho",
    "supervisor",
}
NON_IMPORTED_PREFIXES = ("types-", "pytest-", "flake8-", "mkdocs-", "sphinx-", "sphinxcontrib-", "pylint-")
NON_IMPORTED_SUFFIXES = ("-stubs",)

NODE_BUILTINS = frozenset(
    {
        "assert",
        "async_hooks",
        "buffer",
        "child_process",
        "cluster",
        "console",
        "constants",
        "crypto",
        "dgram",
        "diagnostics_channel",
        "dns",
        "domain",
        "events",
        "fs",
        "http",
        "http2",
        "https",
        "inspector",
        "module",
        "net",
        "os",
        "path",
        "perf_hooks",
        "process",
        "punycode",
        "querystring",
        "readline",
        "repl",
        "stream",
        "string_decoder",
        "sys",
        "timers",
        "tls",
        "trace_events",
        "tty",
        "url",
        "util",
        "v8",
        "vm",
        "wasi",
        "worker_threads",
        "zlib",
        "test",
        "sqlite",
        "sea",
    }
)
# Modules supplied by the host application at runtime rather than installed from the registry.
NODE_HOST_MODULES = frozenset({"vscode"})
# Runtime packages commonly consumed by compiled output, CLIs or configuration rather than imports.
NON_IMPORTED_NODE = {
    "typescript",
    "tslib",
    "core-js",
    "regenerator-runtime",
    "@babel/runtime",
    "@swc/helpers",
    "next",
    "react-scripts",
    "vite",
    "nodemon",
    "ts-node",
    "tsx",
    "pm2",
    "concurrently",
    "cross-env",
    "rimraf",
    "dotenv-cli",
    "husky",
    "lint-staged",
    "prettier",
    "eslint",
}


def python_candidates(module: str) -> set[str]:
    """Plausible normalized distribution keys for a dotted absolute import."""
    parts = module.split(".")
    top = parts[0]
    result: set[str] = {python_key(top)}
    for known in (".".join(parts[:2]), top):
        result.update(IMPORT_TO_DISTRIBUTIONS.get(known, ()))
    if top in NAMESPACE_ROOTS:
        for length in range(2, min(len(parts), 4) + 1):
            result.add(python_key("-".join(parts[:length])))
    key = python_key(top)
    result.update({f"python-{key}", f"py{key}", f"py-{key}", f"{key}-python", f"{key}-py"})
    return result


def python_non_imported(key: str) -> bool:
    return key in NON_IMPORTED_PYTHON or key.startswith(NON_IMPORTED_PREFIXES) or key.endswith(NON_IMPORTED_SUFFIXES)


def node_package(specifier: str) -> str | None:
    """Return the package an import specifier names, or None for relative, builtin and virtual modules."""
    if not specifier or specifier.startswith((".", "/", "#", "~", "@/", "data:", "http:", "https:", "file:")):
        return None
    if specifier.startswith("node:"):
        return None
    if ":" in specifier or "\\" in specifier or "{" in specifier or "$" in specifier:
        return None
    parts = specifier.split("/")
    if parts[0].startswith("@"):
        if len(parts) < 2 or len(parts[0]) < 2 or not parts[1]:  # noqa: PLR2004 - scope plus name.
            return None
        name = f"{parts[0]}/{parts[1]}"
    else:
        name = parts[0]
        if name in NODE_BUILTINS or name in NODE_HOST_MODULES:
            return None
    return name.casefold()

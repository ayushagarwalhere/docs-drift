"""Conservative extraction of checkable claims from prose and Markdown tokens."""

import builtins
import keyword
import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urlsplit

from markdown_it.token import Token

from docs_drift.docs.qualifiers import XREF_PREFIX, context_qualifier
from docs_drift.docs.sections import ParsedDoc, is_historical_span
from docs_drift.ids import make_claim_id
from docs_drift.models import Claim, ClaimKind, Extraction

# Require all-caps separators or a known suffix for environment names; the stoplist
# removes common headings and HTTP vocabulary that happen to look capitalized.
_ENV_STOP = {"HTTP", "JSON", "API", "README", "TODO", "GET", "POST", "NOTE", "MIT"}
_ENV_SUFFIX = ("URL", "KEY", "TOKEN", "PATH", "HOST", "PORT", "DIR")
_HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"})
# Language-level names are never project symbols: builtins, keywords, literals,
# browser/JS globals and protocol vocabulary. Only undotted names are filtered, so
# qualified references such as ``argparse.FileType`` still reach the matcher.
NOT_PROJECT_SYMBOLS = (
    frozenset(dir(builtins))
    | frozenset(keyword.kwlist)
    | {"null", "true", "false", "undefined", "NaN", "nil", "none"}
    | {"fetch", "import", "require", "console", "JSON", "Promise", "window", "document", "globalThis"}
    | {"setTimeout", "setInterval", "export", "async", "await", "alert", "confirm", "localStorage", "sessionStorage"}
    | {"Map", "Set", "WeakMap", "WeakSet", "Array", "Object", "Date", "Math", "RegExp", "Reflect", "Proxy"}
    | {"structuredClone", "requestAnimationFrame", "addEventListener", "navigator"}
    | _HTTP_METHODS
)
_LITERALS = frozenset({"none", "null", "true", "false", "on", "off", "auto", "yes", "no", "undefined"})
# Distinctive executable names used to recognise another tool's flags in prose.
# Names that are also ordinary English words only count when written as code.
_THIRD_PARTY_TOOLS = frozenset({
    "git", "pip", "pipx", "pytest", "uvicorn", "gunicorn", "hypercorn", "daphne", "docker", "kubectl",
    "curl", "wget", "npm", "pnpm", "yarn", "npx", "poetry", "conda", "ssh", "rsync", "grep", "nginx",
    "tox", "nox", "mypy", "ruff", "flake8", "pylint", "isort", "sphinx", "mkdocs", "twine", "brew",
    "sqlite3", "psql", "deno", "heroku", "systemctl", "celery", "zensical", "pre-commit",
})  # fmt: skip
# Servers and runners whose own option reference a deployment section typically reproduces.
_HEADING_TOOLS = frozenset({
    "gunicorn", "uwsgi", "uvicorn", "hypercorn", "waitress", "mod_wsgi", "gevent", "eventlet", "daphne",
    "celery", "nginx", "apache", "systemd", "supervisor",
})  # fmt: skip
_CODE_ONLY_TOOLS = frozenset({"ls", "tar", "node", "black", "make", "find", "cat", "coverage", "bun", "apt", "sed"})
_PLACEHOLDER_FLAG = re.compile(
    r"^--(?:no-)?(?:something|foo|bar|baz|qux|xxx|xyz|my-(?:option|flag)|some-(?:option|flag)|option-name|flag-name)$"
)
PACKAGE_MANAGER_BUILTINS = frozenset({
    "install", "i", "add", "remove", "rm", "uninstall", "ci", "exec", "dlx", "config", "init", "publish",
    "link", "unlink", "update", "up", "upgrade", "outdated", "audit", "pack", "info", "view", "why",
    "list", "ls", "create", "login", "logout", "version", "cache", "store", "set", "global", "dedupe",
})  # fmt: skip
SYSTEM_PATH = re.compile(r"^/(?:etc|usr|var|opt|home|tmp|dev|proc|bin|sbin|lib|srv|root|Users|Library)(?:/|$)")
# Conventional placeholder names (`src/foo`, `pluginname.py`, `my_project/`): present files still
# resolve, but an absent one is the reader's to create.
_PLACEHOLDER_COMPONENT = re.compile(
    r"(?:test_)?(?:foo|bar|baz|qux|quux|spam|eggs|dummy|your[_-]?[a-z]+|"
    r"my[_-]?(?:app|project|package|module|plugin|lib|site|proj|script|file|dir|tool|cli|pkg)\w*|"
    r"(?:plugin|file|project|package|module|app|script|dir|folder|user|some)[_-]?name)",
    re.IGNORECASE,
)
_CREATES_FOLLOWING = re.compile(
    r"\b(?:will|would|should|to)\s+(?:create|generate|produce|contain)\b|\b(?:creates?|generates?)\s+the\s+following\b",
    re.IGNORECASE,
)
# Naming patterns, not files: `docs/changelog/####.{feature,bugfix}.rst`, `XXXX.md`, `NNNN-title.md`.
_TEMPLATE_PATH = re.compile(r"\{[^{}]*,[^{}]*\}|#{2,}|\bX{3,}\b|\bN{3,}\b")
_MIME = re.compile(r"^(?:application|text|image|audio|video|multipart|font|message|model)/[\w.+-]+$")
_ROLE = re.compile(r"(?:\{(?:py:)?(?P<myst>[a-z]+)\}|:(?:py:)?(?P<rst>[a-z]+):)$")
_SYMBOL_ROLES = frozenset({"class", "func", "meth", "exc", "attr", "data", "obj", "const", "function", "method"})
# Well-known filenames are often named generically ("your setup.py") rather than as repository paths.
_GENERIC_FILENAMES = frozenset({
    "setup.py", "setup.cfg", "__init__.py", "__main__.py", "package.json", "pyproject.toml", "changelog.md",
    "changes.md", "history.md", "news.md", "requirements.txt", "tox.ini", "conftest.py", "manage.py",
    "package-lock.json", "tsconfig.json", ".gitignore", ".env",
})  # fmt: skip
_FILENAME_LIKE = re.compile(
    r"\.(?:py|pyi|js|jsx|ts|tsx|json|toml|ya?ml|mdx?|rst|txt|html?|env|cfg|ini|lock|png|jpe?g|gif|svg|csv|xml|sh|"
    r"db|sqlite|zip|gz|whl|log|pem|crt|key|css|scss)$",
    re.IGNORECASE,
)
MIN_SUBCOMMAND_HEADING_LEVEL = 2
_ENV_PATTERN = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$|^[A-Z][A-Z0-9]*(?:URL|KEY|TOKEN|PATH|HOST|PORT|DIR)$")
# Flag assignments are accepted so the value can be separated as example data.
_FLAG = re.compile(r"^--?[A-Za-z][\w-]*(?:=.*|\s+(?:<[^>]*>|\[[^\]]*\]|\{[^}]*\}|[A-Z][A-Z0-9_]*))?$")
# Endpoints are limited to common HTTP methods and absolute paths; parameter
# names are normalized separately so `/users/{id}` matches `/users/:user_id`.
_ENDPOINT = re.compile(r"^(?:(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+)?(/[^\s]*)$", re.IGNORECASE)
_PATH_PARAM = re.compile(r"\{[^}/]+\}|:[A-Za-z_]\w*|<[A-Za-z_]\w*>")
# This short extension set avoids treating arbitrary dotted prose as a file path.
_FILE_EXT = re.compile(
    r"\.(?:py|pyi|pyx|pxd|js|jsx|ts|tsx|json|toml|ya?ml|mdx?|rst|txt|html?|env)(?:$|[?#])", re.IGNORECASE
)
_IDENTIFIER = re.compile(r"^(?:[A-Za-z_]\w*(?:\(\))?|[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)$")
_CAMEL_OR_SNAKE = re.compile(r"^(?:[a-z]+(?:_[a-z0-9]+)+|[A-Z][a-zA-Z0-9]+)$")
_SENTENCE = re.compile(r".+?(?:[.!?](?=\s|$)|$)")
_DEFAULT = re.compile(
    r"\bdefaults?\s+to\s+`?(?P<to>[^``,;)]+)`?|"
    r"\bdefault\s*(?::|is)\s*`?(?P<colon>[^``,;)]+)`?|"
    r"\(\s*default\s+`?(?P<paren>[^`)]+)`?\s*\)",
    re.IGNORECASE,
)
_SUBCOMMAND = re.compile(r"\b(?:the\s+)?`?([a-z][\w-]*)`?\s+(?:sub)?command\b", re.IGNORECASE)
_COMMAND_NAME = re.compile(r"`([a-z][\w-]*)`\s+subcommand\b", re.IGNORECASE)
_TOOL_COMMAND = re.compile(
    r"^(?P<tool>npm\s+run|pnpm(?:\s+run)?|yarn(?:\s+run)?|make|just|uv\s+run|poetry\s+run)\s+(?P<target>[\w:.-]+)"
    r"(?:\s+.*)?$",
    re.IGNORECASE,
)
_NPX_COMMAND = re.compile(r"^npx\s+[^\s]+(?:\s+.*)?$", re.IGNORECASE)
_WRAPPED_ENV = re.compile(
    r"^(?:process\.env\.(?P<process>[A-Za-z_]\w*)|os\.environ\[\s*['\"](?P<environ>[A-Za-z_]\w*)['\"]\s*\]|os\.getenv\(\s*['\"](?P<getenv>[A-Za-z_]\w*)['\"]\s*\)|\$(?:\{(?P<braced>[A-Za-z_]\w*)\}|(?P<shell>[A-Za-z_]\w*)))$"
)


@dataclass(frozen=True)
class ExtractionResult:
    """Claims plus the number of candidate mentions suppressed as historical."""

    claims: list[Claim]
    historical_skipped: int


@dataclass(frozen=True)
class _Candidate:
    kind: ClaimKind
    value: str
    line: int
    column: int
    raw_text: str
    extraction: Extraction
    confidence: float
    expected_default: str | None = None
    qualifier: str | None = None


def _environment(value: str) -> str | None:
    if re.fullmatch(r"[A-Z][A-Z0-9_]+=\S+", value):
        value = value.partition("=")[0]
    wrapped = _WRAPPED_ENV.fullmatch(value)
    if wrapped:
        return next(part for part in wrapped.groupdict().values() if part is not None)
    # A bare suffix word (`URL`, `KEY`, `PATH`) names a concept or class far more often than a variable.
    if value in _ENV_STOP or value in _ENV_SUFFIX or value in _HTTP_METHODS:
        return None
    if not _ENV_PATTERN.fullmatch(value):
        return None
    # Exclude unseparated acronym-like words; suffix recognition is handled above.
    if "_" not in value and not value.endswith(_ENV_SUFFIX):
        return None
    return value


def _explicit_api_reference(value: str, sentence: str) -> bool:
    """Disambiguate dotted API names using their own local grammatical context."""
    if "." not in value or not _IDENTIFIER.fullmatch(value):
        return False
    quoted = r"[`'\"]*" + re.escape(value) + r"[`'\"]*"
    role = r"(?:module|attribute|property|object|instance)"
    return bool(
        re.search(r"\b" + role + r"\s+(?:named\s+)?" + quoted, sentence, re.I)
        or re.search(quoted + r"\s+" + role + r"\b", sentence, re.I)
        or re.search(
            r"\b(?:functions?|methods?)\s+(?:provided\s+by|available\s+on|in|on|of|from)\s+" + quoted,
            sentence,
            re.I,
        )
    )


def _classify(  # noqa: PLR0911
    value: str, *, api_reference: bool = False, sentence: str = ""
) -> tuple[ClaimKind, str, float] | None:
    """Prefer narrow lexical shapes; prose context only upgrades clear identifiers to symbols."""
    # Shell-completion demos write `--[TAB][TAB]`; the markers are keystrokes, not text.
    value = re.sub(r"\s*\[TAB\]", "", value).rstrip("…").strip()
    if not value:
        return None
    command = _TOOL_COMMAND.fullmatch(value)
    if command:
        tool = command.group("tool").casefold().split()
        target = command.group("target")
        if tool[0] in {"pnpm", "yarn"} and len(tool) == 1 and target.casefold() in PACKAGE_MANAGER_BUILTINS:
            return None  # `pnpm install` is the package manager's own command, not a project script.
        kind = ClaimKind.make_target if tool[0] in {"make", "just"} else ClaimKind.package_script
        return kind, target, 0.9
    if _NPX_COMMAND.fullmatch(value):
        # Preserve the full invocation so command-specific flags remain attached to their owning tool.
        return ClaimKind.code_command, value, 0.8
    env = _environment(value)
    if env:
        # The explicit 'set' extractor already retains its name/value pair.
        if "=" in value:
            return (
                None
                if re.search(r"\bset\s+`" + re.escape(value) + r"`", sentence, re.I)
                else (ClaimKind.env_var, env, 0.85)
            )
        return ClaimKind.env_var, env, 0.95 if env == value else 0.9
    if re.fullmatch(r"[a-z][\w-]*", value) and _names_subcommand(value, sentence):
        return ClaimKind.cli_command, value, 0.8
    endpoint = _ENDPOINT.fullmatch(value)
    if endpoint and not SYSTEM_PATH.match(endpoint.group(2)):
        method, path = endpoint.groups()
        normalized = _PATH_PARAM.sub("{param}", path)
        return ClaimKind.endpoint, f"{method.upper()} {normalized}" if method else normalized, 0.95
    pair = _flag_pair(value)
    if pair:
        return ClaimKind.cli_flag, pair, 0.9
    if _FLAG.fullmatch(value):
        name = re.split(r"[=\s]", value, maxsplit=1)[0]
        # A short single-dash option is weaker because prose often uses hyphenated examples.
        return ClaimKind.cli_flag, name, 0.9 if name.startswith("--") else 0.65
    if value.startswith(("http://", "https://", "www.", "#!")):
        return None
    return _path_or_symbol(value, api_reference=api_reference, sentence=sentence)


def _names_subcommand(value: str, sentence: str) -> bool:
    """Require the word (sub)command next to the span itself, not anywhere in the sentence."""
    quoted = r"[`'\"]+" + re.escape(value) + r"[`'\"]+"
    return bool(
        re.search(quoted + r"\s+subcommands?\b", sentence, re.IGNORECASE)
        or re.search(r"\bsubcommand(?:\s+(?:named|called))?\s+" + quoted, sentence, re.IGNORECASE)
    )


def _flag_pair(value: str) -> str | None:
    """Read `-d/--database`, `-d, --database` and `--flag/--no-flag` notation as the long option."""
    parts = re.split(r"\s*(?:/|,|\|)\s*", value)
    if len(parts) < 2 or not all(re.fullmatch(r"--?[A-Za-z][\w-]*", part) for part in parts):  # noqa: PLR2004
        return None
    return next((part for part in parts if part.startswith("--")), None)


def _config_noun_adjacent(value: str, sentence: str) -> bool:
    """A configuration noun must qualify the span itself, as in "the `x` setting" or "key `x`"."""
    quoted = r"[`'\"]+" + re.escape(value) + r"[`'\"]+"
    noun = r"(?:setting|config(?:uration)?\s+key|field)"
    # "by setting `root_path`" uses the verb; only "the setting `x`" (with a determiner) is the noun.
    return bool(
        re.search(quoted + r"\s+" + noun + r"s?\b", sentence, re.IGNORECASE)
        or re.search(
            r"(?:\b(?:the|a|an|this|each)\s+setting|\bconfig(?:uration)?\s+key|\bfield)\s+(?:named\s+|called\s+)?"
            + quoted,
            sentence,
            re.IGNORECASE,
        )
    )


def _path_or_symbol(value: str, *, api_reference: bool, sentence: str) -> tuple[ClaimKind, str, float] | None:  # noqa: PLR0911
    if _explicit_api_reference(value, sentence):
        return ClaimKind.symbol, value, 0.8
    if value.removesuffix("()") in NOT_PROJECT_SYMBOLS or value.casefold() in _LITERALS:
        return None
    if (
        re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", value)
        and not _FILENAME_LIKE.search(value)
        and _config_noun_adjacent(value, sentence)
    ):
        return ClaimKind.config_key, value, 0.75
    call = re.fullmatch(r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\(.*\)", value)
    if call:
        if call[1] in NOT_PROJECT_SYMBOLS:
            return None
        return ClaimKind.symbol, call[1], 0.8 if api_reference else 0.72
    if value.startswith("-"):
        return None  # `-foo.txt` illustrates option parsing; leading dashes never start a checkout path.
    if "::" in value:
        return None  # pytest node ids (`tests/test_x.py::test_y`) and Rust/C++ paths, not checkout files.
    # Known source and documentation extensions distinguish paths from dotted symbols.
    file_value = re.sub(r":\d+(?::\d+)?$", "", value)
    if (
        not re.search(r"\s|[*?=]|^\.[A-Za-z]+$", value)  # `enctype=multipart/form-data` is an attribute
        and not re.search(r"\b(?:never commit|do not commit|glob|ignore)\b", sentence, re.I)
        and (value.startswith(".env.") or "/" in value or _FILE_EXT.search(file_value))
        and not value.startswith(("http://", "https://"))
    ):
        return ClaimKind.file_path, file_value, 0.88
    # A code command's own target is useful; flags belonging to unrelated tools are not.
    symbol_context = api_reference or re.search(r"\b(?:function|class|method|call|returns?)\b", sentence, re.IGNORECASE)
    # Factory context establishes the role independently of the identifier's verb prefix.
    application_factory = re.search(
        r"\b(?:create|build|construct)\s+the\s+application\s+with\b", sentence, re.IGNORECASE
    )
    if _IDENTIFIER.fullmatch(value) and (
        "(" in value
        or "." in value
        or (symbol_context and _CAMEL_OR_SNAKE.fullmatch(value))
        or (application_factory and _CAMEL_OR_SNAKE.fullmatch(value))
    ):
        return ClaimKind.symbol, value.removesuffix("()"), 0.72 if not api_reference else 0.8
    return None


def path_context(  # noqa: PLR0911
    sentence: str, value: str = "", project_names: frozenset[str] = frozenset()
) -> str | None:
    """Retain explicitly illustrative/output paths without asserting checkout existence."""
    if re.match(r"^[A-Za-z][\w+.-]*://|^@(?:[\w.-]+/|/)", value):
        return "non-checkout-path"
    # Home, environment-macro and system locations live on the reader's machine, not in the checkout.
    if (
        value.startswith(("~", "$", "%", "-"))
        or re.search(r"\$\w+\$|\$\{[^}]*\}|%\w+%|<[\w-]+>", value)
        or SYSTEM_PATH.match(value)
        or _MIME.fullmatch(value)
        or _TEMPLATE_PATH.search(value)
    ):
        return "non-checkout-path"
    quoted = r"[`'\"]+" + re.escape(value) + r"[`'\"]+"
    if value and re.search(
        r"\b(?:create|generate|write|save)\s+(?:(?:a|an|the|new)\s+)?"
        r"(?:(?:file|directory|directory tree|folder)\s+)?(?:(?:named|called)\s+)?"
        + quoted
        # "**Create provider module** (`scalene/scalene-gui/newprovider.ts`)": the path names the new file.
        + r"|\bcreate\s+(?:(?:a|an|the|new|your)\s+)?(?:[\w-]+[*_]*\s+){0,3}\(\s*"
        + quoted,
        sentence,
        re.I,
    ):
        return "non-checkout-path"
    if value and re.search(
        r"\b(?:copy|paste|put|save|add|move|place)\b(?:[^.]|\.(?=\w)){0,80}\b(?:to|into|in|as)\s+(?:the\s+)?(?:a\s+(?:new\s+)?file\s+)?"
        + quoted
        + r"|\b(?:a|new)\s+file\s+(?:named|called)\s+"
        + quoted,
        sentence,
        re.I,
    ):
        return "non-checkout-path"
    if value and (
        _hypothetical_path(sentence, value, quoted, project_names)
        or any(_PLACEHOLDER_COMPONENT.fullmatch(part.split(".", 1)[0]) for part in value.split("/") if part)
    ):
        return "ambiguous-path"
    if re.search(
        r"\b(?:generated(?=\s+`)|generated (?:reports?|files?|artifacts?|(?:debug )?logs?)|output (?:files?|paths?)|"
        r"example (?:manifest|filename|path)|placeholder|shorthand|upstream source|external repository|"
        # "`contributor-names.json` is generated by `pnpm docs:contributors`"; "in these repos you can find".
        r"(?:is|are) generated\b|generated by|"
        r"in (?:these|those|their|other|its|each) (?:repos?|repositories|projects?))\b",
        sentence,
        re.I,
    ):
        return "non-checkout-path"
    if (
        "/" in value
        and not re.search(r"\.[A-Za-z0-9]{1,8}$", value)
        and not re.search(r"\b(?:file|directory|folder|path)\b", sentence, re.I)
    ):
        return "ambiguous-path"
    return None


def _hypothetical_path(sentence: str, value: str, quoted: str, project_names: frozenset[str]) -> bool:
    """Hedged, generic or reader-owned files: existence may still be confirmed, absence proves nothing."""
    basename = value.rsplit("/", 1)[-1]
    if "/" not in value and basename.casefold() in _GENERIC_FILENAMES:
        return True
    stem = basename.casefold().lstrip(".").split(".", 1)[0]
    if (
        "/" not in value
        and stem
        and any(name and re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", stem) for name in project_names)
    ):
        return True  # `datasette.yaml`, `.np-config.json`: the reader's config file named after the tool.
    return bool(
        re.search(
            r"\b(?:e\.g\.|for example|for instance|let['\u2019]s say|suppose|imagine|say you have|"
            r"your (?:own |project['\u2019]s |package['\u2019]s )?\w+|(?:home|user|package|project) directory|"
            r"in the user['\u2019]s|it (?:can|may|might) be (?:useful|helpful|convenient)|such as|"
            r"you (?:can|could|may|might) (?:store|put|keep|place|create|add|organi[sz]e)|"
            r"consider (?:creating|adding|putting|storing|using)|"
            r"would (?:then )?be (?:located|stored|placed|found|written|created))",
            sentence,
            re.I,
        )
        or re.search(r"\ban?\s+(?:[\w-]+\s+){0,3}file,?\s+(?:named\s+|called\s+)?" + quoted, sentence, re.I)
        # "store files in a `tests/resources` folder": the indefinite article makes it any such folder.
        or re.search(r"\ban?\s+" + quoted + r"\s+(?:file|folder|directory)\b", sentence, re.I)
    )


def _line_column(text: str, raw: str, cursor: list[int]) -> tuple[int, int]:
    """Recover positions from source text because markdown-it inline tokens omit maps."""
    needle = f"`{raw}`"
    start = text.find(needle, cursor[0])
    if start < 0:
        start = text.find(raw, cursor[0])
    if start < 0:
        return 1, 1
    cursor[0] = start + len(needle)
    return text.count("\n", 0, start) + 1, start - text.rfind("\n", 0, start)


def _sentence_for_line(text: str | list[str], line: int, column: int) -> str:
    lines = text.splitlines() if isinstance(text, str) else text
    if not (1 <= line <= len(lines)):
        return ""
    source = lines[line - 1]
    return next(
        (match.group() for match in _SENTENCE.finditer(source) if match.start() <= column <= match.end()), source
    )


def _qualifier(section_heading: str, sentence: str, heading_level: int) -> str | None:
    """Use only explicit command labels in the heading or sentence to avoid guessing ownership."""
    heading_match = re.fullmatch(r"\s*([\w-]+)\s*", section_heading)
    if heading_match and heading_level >= MIN_SUBCOMMAND_HEADING_LEVEL:
        return heading_match.group(1)
    match = _SUBCOMMAND.search(sentence)
    if match:
        return match.group(1)
    command = _COMMAND_NAME.search(sentence)
    if command:
        return command.group(1)
    # In compact bullets, an explicit command name directly before "accepts" is still clear ownership.
    accepts = re.search(r"`([a-z][\w-]*)`\s+accepts\b", sentence, re.IGNORECASE)
    return accepts.group(1) if accepts else None


def _explicit_role(children: list[Token], index: int) -> str | None:
    """Return the MyST/Sphinx role written directly before a code span, such as {class}`Name`."""
    if index == 0 or children[index - 1].type != "text":
        return None
    match = _ROLE.search(children[index - 1].content)
    return (match.group("myst") or match.group("rst")) if match else None


def _role_symbol(raw: str) -> str | None:
    """Sphinx role targets may carry `~`/`!` markers, a display title and call parentheses."""
    target = re.sub(r"^.*<([^>]+)>$", r"\1", raw.strip()).lstrip("~!").removesuffix("()")
    if not _IDENTIFIER.fullmatch(target) or target in NOT_PROJECT_SYMBOLS:
        return None
    return target


def _external_host(href: str, project_names: frozenset[str]) -> str | None:
    """Links to reference material elsewhere make the span another project's name, not this one's."""
    try:
        parts = urlsplit(href)
    except ValueError:
        return None
    host = (parts.hostname or "").casefold()
    if parts.scheme not in {"http", "https"} or not host:
        return None
    if host in {"github.com", "gitlab.com", "bitbucket.org", "localhost", "127.0.0.1"}:
        return None  # Source hosting links commonly point at the project's own code.
    if any(name and len(name) > 2 and name in host for name in project_names):  # noqa: PLR2004
        return None  # The project's own documentation site.
    return host


def _links_elsewhere(href: str) -> bool:
    """A web link that is not a file view: `httpie/debian.httpie.io` linking to that repository.

    Link text that names a path in a checkout links to its blob/tree/raw view; any other web
    target (a repository, an issue, a site) makes the span the name of something outside.
    """
    try:
        parts = urlsplit(href)
    except ValueError:
        return False
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return False
    return not re.search(r"/(?:blob|tree|raw|src|-/blob|-/tree)/", parts.path)


def _mentions_name(text: str, name: str) -> bool:
    return re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", text, re.IGNORECASE) is not None


def third_party_tool(context: str, code_text: str, project_names: frozenset[str], heading: str = "") -> str | None:
    """Find another tool named as the owner of nearby flags; the project's own names always win.

    ``heading`` is the section's heading trail: a section titled "Gunicorn" documents Gunicorn's
    options even when a parent heading names the project.
    """
    if any(name and len(name) > 1 and _mentions_name(context, name) for name in project_names):
        return None
    for tool in sorted(_HEADING_TOOLS):
        if tool not in project_names and _mentions_name(heading, tool):
            return tool
    owner = _PROGRAM_OWNER.search(context)
    if owner and owner.group(1).casefold() not in project_names:
        return owner.group(1).casefold()
    for tool in sorted(_THIRD_PARTY_TOOLS):
        if tool not in project_names and _mentions_name(context, tool):
            return tool
    for span in re.findall(r"`+([^`]+)`+", code_text):
        first = span.split(maxsplit=1)[0].casefold() if span.split() else ""
        if any(name and len(name) > 1 and _mentions_name(span, name) for name in project_names):
            continue  # `pip install <project>` introduces the project, not another tool.
        if first in (_THIRD_PARTY_TOOLS | _CODE_ONLY_TOOLS) and first not in project_names:
            return first
    return None


_PROGRAM_OWNER = re.compile(
    # "the `serve` command" usually names the project's own subcommand, so only program-like nouns count.
    r"\bthe\s+`([\w.-]+)`\s+(?:program|tool|utility|binary|executable)\b",
    re.IGNORECASE,
)


def section_program(parsed: ParsedDoc, line: int, project_names: frozenset[str]) -> str:
    """A section that introduces "the `ls` program" is about that program's options, not the project's."""
    section = parsed.line_to_section.get(line)
    if section is None:
        return ""
    text = "\n".join(parsed.lines[section.start_line - 1 : section.end_line])
    owners = {match.casefold() for match in _PROGRAM_OWNER.findall(text)}
    foreign = sorted(owner for owner in owners if owner not in project_names)
    return f"the `{foreign[0]}` program" if foreign and owners <= set(foreign) else ""


def preceding_text(lines: list[str], start: int, line: int, column: int) -> str:
    """Paragraph text from its first line up to (not including) the span at ``line``/``column``."""
    if not 0 < line <= len(lines):
        return ""
    return "\n".join([*lines[start - 1 : line - 1], lines[line - 1][: max(0, column - 1)]])


def _paragraph_sentence(lines: list[str], start: int, end: int, line: int, column: int) -> str:
    """Rebuild the sentence around a span when a paragraph wraps it over several lines."""
    block = lines[start - 1 : end]
    if not block:
        return ""
    text = " ".join(block)
    offset = sum(len(item) + 1 for item in block[: line - start]) + column - 1
    return next((m.group() for m in _SENTENCE.finditer(text) if m.start() <= offset <= m.end()), text)


def _list_lead_in(lines: list[str], line: int) -> str:
    """For a list item, return the introducing line ending in ':' (as in "short names to `tar`:")."""
    if not re.match(r"\s*(?:[-*+]|\d+[.)])\s", lines[line - 1] if 0 < line <= len(lines) else ""):
        return ""
    for index in range(line - 2, max(-1, line - 14), -1):
        text = lines[index].strip()
        if not text or re.match(r"(?:[-*+]|\d+[.)])\s", text) or lines[index].startswith(("  ", "\t")):
            continue
        return text if text.endswith(":") else ""
    return ""


def _context_qualifier(  # noqa: PLR0913
    kind: ClaimKind,
    value: str,
    *,
    sentence: str,
    paragraph: str,
    lead_in: str,
    link_host: str | None,
    project_names: frozenset[str],
    heading: str = "",
) -> str | None:
    if kind == ClaimKind.cli_flag and _PLACEHOLDER_FLAG.fullmatch(value):
        return context_qualifier("placeholder")
    if link_host and kind in {
        ClaimKind.symbol,
        ClaimKind.cli_flag,
        ClaimKind.cli_command,
        ClaimKind.env_var,
        ClaimKind.config_key,
    }:
        return context_qualifier("external-link", link_host)
    if kind in {ClaimKind.cli_flag, ClaimKind.cli_command}:
        tool = _trailing_owner(sentence, value, project_names) or third_party_tool(
            f"{sentence} {lead_in}", f"{paragraph} {lead_in}", project_names, heading
        )
        if tool:
            return context_qualifier("third-party", tool)
    return None


_TRAILING_OWNER = re.compile(
    r"(?:\s*(?:,|/|\bor\b|\band\b)\s*`+[^`]+`+)*\s+(?:options?|flags?|switch(?:es)?|arguments?)\s+"
    r"(?:of|from|in|for|to)\s+(?:the\s+)?(?:GNU\s+)?`?(?P<tool>[A-Za-z][\w.+-]*)`?",
    re.IGNORECASE,
)
_NOT_TOOLS = frozenset({
    "this", "that", "these", "those", "your", "our", "each", "every", "a", "an", "its", "their", "any", "all",
    "command", "commands", "subcommand", "subcommands", "cli", "tool", "program", "same", "following", "other",
    "both", "either", "it", "them", "which", "example", "instance", "use",
})  # fmt: skip


def _trailing_owner(sentence: str, value: str, project_names: frozenset[str]) -> str | None:
    """`the ``-k`` or ``--keep-going`` option of GNU Make`: the sentence names the option's program."""
    for span in re.finditer(r"`+" + re.escape(value) + r"(?:[\s=][^`]*)?`+", sentence):
        owner = _TRAILING_OWNER.match(sentence, span.end())
        if owner is None:
            continue
        tool = owner.group("tool").rstrip(".").casefold()
        known = tool in _THIRD_PARTY_TOOLS | _CODE_ONLY_TOOLS | _HEADING_TOOLS or "GNU " in owner.group()
        if tool in _NOT_TOOLS or not known or any(tool == name.casefold() for name in project_names):
            return None
        return tool
    return None


def _inline_candidates(  # noqa: PLR0912, PLR0915
    parsed: ParsedDoc, project_names: frozenset[str] = frozenset()
) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    cursor = [0]
    block_line: int | None = None
    block_end: int | None = None
    for token in parsed.tokens:
        if token.type == "paragraph_open" and token.map is not None:
            block_line, block_end = token.map[0] + 1, token.map[1]
            continue
        if token.type == "paragraph_close":
            block_line = block_end = None
            continue
        if token.type != "inline" or token.children is None:
            continue
        link_host: str | None = None
        link_href = ""
        for index, child in enumerate(token.children):
            if child.type == "link_open":
                link_href = str(child.attrGet("href") or "")
                link_host = _external_host(link_href, project_names)
                continue
            if child.type == "link_close":
                link_host = None
                link_href = ""
                continue
            if child.type != "code_inline":
                continue
            raw = child.content
            line, column = _line_column(parsed.text, raw, cursor)
            if token.map is not None and line < token.map[0] + 1:
                line = token.map[0] + 1
            elif block_line is None:
                continue
            section = parsed.line_to_section.get(line)
            if section is None:
                continue
            sentence = _sentence_for_line(parsed.text, line, column)
            role = _explicit_role(token.children, index)
            if role is not None and role not in _SYMBOL_ROLES:
                continue  # {ref}, {doc}, {mod} and similar roles name documents or modules, not code symbols.
            api_reference = any("api reference" in heading.casefold() for heading in section.heading_path)
            kind_and_value: tuple[ClaimKind, str, float] | None
            if role is not None:
                # An explicit cross-reference role is an unambiguous API reference, whatever the prose says.
                target = _role_symbol(raw)
                kind_and_value = (ClaimKind.symbol, target, 0.9) if target else None
            else:
                kind_and_value = _classify(raw, api_reference=api_reference, sentence=sentence)
            if kind_and_value is None:
                continue
            kind, value, confidence = kind_and_value
            if kind == ClaimKind.config_key and value.casefold() in project_names:
                continue  # "a top-level `np` field in package.json" names the tool, not one of its settings.
            qualifier = (
                _qualifier(section.heading, sentence, section.heading_level) if kind == ClaimKind.cli_flag else None
            )
            start = block_line or line
            end = block_end or line
            # Only text before the span can introduce another tool ("output of `git blame` ... `--ignore-revs-file`").
            paragraph = preceding_text(parsed.lines, start, line, column)
            full_sentence = _paragraph_sentence(parsed.lines, start, end, line, column) if block_line else sentence
            if kind == ClaimKind.file_path:
                qualifier = path_context(full_sentence, value, project_names)
                if qualifier is None and line > 1 and parsed.lines[line - 2].rstrip().endswith(":"):
                    qualifier = path_context(parsed.lines[line - 2], value, project_names)
                if qualifier is None and _CREATES_FOLLOWING.search(_list_lead_in(parsed.lines, line)):
                    # "... will create the following files:" introduces files of the reader's new project.
                    qualifier = "ambiguous-path"
                if _links_elsewhere(link_href):
                    qualifier = "non-checkout-path"
            lead_in = _list_lead_in(parsed.lines, line)
            if kind in {ClaimKind.cli_flag, ClaimKind.cli_command}:
                lead_in = f"{lead_in} {section_program(parsed, line, project_names)}"
            if role is not None and kind == ClaimKind.symbol:
                qualifier = XREF_PREFIX + role
            contextual = _context_qualifier(
                kind,
                value,
                sentence=full_sentence,
                paragraph=paragraph,
                lead_in=lead_in,
                link_host=link_host,
                project_names=project_names,
                heading=" / ".join(section.heading_path),
            )
            candidates.append(
                _Candidate(
                    kind,
                    value,
                    line,
                    column,
                    raw,
                    Extraction.inline_code,
                    confidence,
                    qualifier=contextual or qualifier,
                )
            )
    return candidates


def _table_cells(tokens: list[Token]) -> list[tuple[list[tuple[str, int]], list[list[tuple[str, int]]]]]:
    """Read Markdown table cell inline text and source lines without interpreting HTML."""
    tables = []
    index = 0
    while index < len(tokens):
        if tokens[index].type != "table_open":
            index += 1
            continue
        index += 1
        header: list[tuple[str, int]] = []
        rows: list[list[tuple[str, int]]] = []
        current: list[tuple[str, int]] | None = None
        row_line = 1
        in_head = False
        while index < len(tokens) and tokens[index].type != "table_close":
            token = tokens[index]
            if token.type == "thead_open":
                in_head = True
            elif token.type == "thead_close":
                in_head = False
            elif token.type == "tr_open":
                current = []
                row_line = token.map[0] + 1 if token.map is not None else 1
            elif token.type == "inline" and current is not None:
                line = token.map[0] + 1 if token.map is not None else row_line
                current.append((token.content.strip(), line))
            elif token.type == "tr_close" and current is not None:
                if in_head:
                    header = current
                else:
                    rows.append(current)
                current = None
            index += 1
        if header:
            tables.append((header, rows))
        index += 1
    return tables


def _table_candidates(parsed: ParsedDoc) -> list[_Candidate]:
    candidates = []
    header_words = re.compile(r"\b(?:flag|option|argument|variable|env|setting|key|default|endpoint)\b", re.IGNORECASE)
    for header, rows in _table_cells(parsed.tokens):
        names = [cell.casefold() for cell, _ in header]
        if not any(header_words.search(cell) for cell in names):
            continue
        # The first column is the documented identifier; only a headed default column is a claimed value.
        default_index = next((i for i, name in enumerate(names) if name.strip() in {"default", "default value"}), None)
        first_heading = names[0] if names else ""
        for row in rows:
            if not row:
                continue
            raw, line = row[0]
            raw = raw.strip("`")
            normalized_value: str | None = None
            section = parsed.line_to_section.get(line)
            if section is None:
                continue
            sentence = " ".join(cell for cell, _ in row)
            if "flag" in first_heading or "option" in first_heading or "argument" in first_heading:
                classified = _classify(raw, sentence=sentence)
                kind = ClaimKind.cli_flag if classified and classified[0] == ClaimKind.cli_flag else None
                # `--auth, -a` and `--port <int>` cells name the option `--auth` / `--port`.
                normalized_value = classified[1] if classified and kind else None
            elif any(term in first_heading for term in ("env", "variable")):
                env = _environment(raw.strip("`"))
                kind = ClaimKind.env_var if env else None
                raw = raw.strip("`")
            elif "endpoint" in first_heading:
                classified = _classify(raw.strip("`"))
                kind = ClaimKind.endpoint if classified and classified[0] == ClaimKind.endpoint else None
                normalized_value = classified[1] if classified else None
            elif "setting" in first_heading or "key" in first_heading:
                config_key = re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", raw.strip("`"))
                kind = ClaimKind.config_key if config_key else None
                raw = raw.strip("`")
            else:
                classified = _classify(raw.strip("`"), sentence=sentence)
                kind = classified[0] if classified else None
            if kind is None:
                continue
            value = normalized_value or raw.strip("`")
            default = row[default_index][0].strip() if default_index is not None and default_index < len(row) else None
            if default and default.startswith("`") and not re.fullmatch(r"`[^`]+`", default):
                default = None
            default = default.strip("` ") if default else None
            default = default if default and default.casefold() not in {"none", "n/a", "â€”", "-"} else None
            candidates.append(_Candidate(kind, value, line, 1, raw, Extraction.table, 0.88, expected_default=default))
    return candidates


def _code_block_candidates(parsed: ParsedDoc) -> list[_Candidate]:
    """Recognize only known project-command shapes in shell-labeled fences; never run them."""
    candidates = []
    for token in parsed.tokens:
        if token.type != "fence" or token.map is None:
            continue
        language = token.info.strip().casefold().split(maxsplit=1)[0] if token.info.strip() else ""
        if language not in {"sh", "shell", "bash", "zsh", "console"}:
            continue
        source_lines = token.content.splitlines()
        # With prompts present, unprompted lines are program output, never commands or paths.
        prompt_mode = any(line.lstrip().startswith(("$ ", "> ")) for line in source_lines)
        for offset, source in enumerate(source_lines):
            command = source.strip()
            if prompt_mode and not command.startswith(("$ ", "> ")):
                continue
            classified = _classify(command)
            if classified is None:
                continue
            kind, value, confidence = classified
            candidates.append(
                _Candidate(
                    kind,
                    value,
                    token.map[0] + offset + 2,
                    len(source) - len(source.lstrip()) + 1,
                    command,
                    Extraction.code_block,
                    confidence,
                )
            )
    return candidates


def _prose_candidates(parsed: ParsedDoc, candidates: list[_Candidate]) -> list[_Candidate]:
    result = []
    by_line: dict[int, list[_Candidate]] = {}
    for candidate in candidates:
        by_line.setdefault(candidate.line, []).append(candidate)
    for line, source in enumerate(parsed.text.splitlines(), 1):
        section = parsed.line_to_section.get(line)
        if section is None:
            continue
        for match in _DEFAULT.finditer(source):
            value = next(group for group in match.groupdict().values() if group is not None).strip()
            if not value:
                continue  # RST double-backtick defaults can otherwise yield a whitespace-only span.
            sentence = next(
                (item for item in _SENTENCE.finditer(source) if item.start() <= match.start() <= item.end()), None
            )
            sentence_start = sentence.start() if sentence else 0
            nearby = [
                candidate
                for candidate in by_line.get(line, [])
                if sentence_start <= candidate.column - 1 <= match.start()
            ]
            if not nearby:
                # Defaults without a named claim have weak value but remain useful for downstream linking.
                result.append(
                    _Candidate(
                        ClaimKind.default_value,
                        value,
                        line,
                        match.start() + 1,
                        match.group(),
                        Extraction.prose_pattern,
                        0.45,
                    )
                )
            else:
                result.append(
                    _Candidate(
                        ClaimKind.default_value,
                        value,
                        line,
                        match.start() + 1,
                        match.group(),
                        Extraction.prose_pattern,
                        0.5,
                        qualifier=nearby[-1].value,
                    )
                )
        for match in re.finditer(r"\bset\s+`([^`=]+)=([^`]+)`(?:\s+to\b)?", source, re.IGNORECASE):
            name, example = match.groups()
            # This targeted pattern preserves the setting name while treating its assignment as an example.
            # Only environment-shaped names are variables; `set count=True` is a keyword argument.
            if _environment(name.strip()) is not None:
                result.append(
                    _Candidate(
                        ClaimKind.env_var,
                        name,
                        line,
                        match.start(1) + 1,
                        match.group(1),
                        Extraction.prose_pattern,
                        0.55,
                    )
                )
            result.append(
                _Candidate(
                    ClaimKind.default_value,
                    example,
                    line,
                    match.start(2) + 1,
                    example,
                    Extraction.prose_pattern,
                    0.4,
                    qualifier=name,
                )
            )
        for match in re.finditer(r"\bset\s+`([^`=]+)`\s+to\s+`([^`]+)`", source, re.IGNORECASE):
            name, example = match.groups()
            # A stated assignment is an example value, so preserve the setting name as its qualifier.
            result.append(
                _Candidate(
                    ClaimKind.default_value,
                    example,
                    line,
                    match.start(2) + 1,
                    example,
                    Extraction.prose_pattern,
                    0.4,
                    qualifier=name,
                )
            )
    return result


def extract_claims(parsed: ParsedDoc, *, project_names: frozenset[str] = frozenset()) -> ExtractionResult:
    """Extract high-signal spans first, tables next, then only explicit low-confidence prose forms.

    ``project_names`` (executables, distribution and package names, casefolded) lets context
    rules tell this project's tools apart from third-party ones named in the same prose.
    """
    candidates = _inline_candidates(parsed, project_names)
    # API-reference context is inherited from the section path and checked per span during classification.
    candidates.extend(_table_candidates(parsed))
    candidates.extend(_code_block_candidates(parsed))
    candidates.extend(_prose_candidates(parsed, candidates))
    table_mentions = {
        (candidate.line, candidate.raw_text.strip("`").strip())
        for candidate in candidates
        if candidate.extraction == Extraction.table
    }
    # A table's header supplies stronger kind context than the same first-column inline span.
    candidates = [
        candidate
        for candidate in candidates
        if not (
            candidate.extraction == Extraction.inline_code and (candidate.line, candidate.raw_text) in table_mentions
        )
    ]
    selected: list[_Candidate] = []
    skipped = 0
    seen: set[tuple[ClaimKind, str, int, int]] = set()
    for candidate in sorted(candidates, key=lambda item: (item.line, item.column, item.kind.value, item.value)):
        section = parsed.line_to_section.get(candidate.line)
        if section is None:
            continue
        if is_historical_span(section, candidate.line):
            skipped += 1
            continue
        key = (candidate.kind, candidate.value, candidate.line, candidate.column)
        if key in seen:
            continue
        seen.add(key)
        selected.append(candidate)
    occurrences: Counter[tuple[str, ClaimKind, str]] = Counter()
    claims = []
    for candidate in selected:
        section = parsed.line_to_section[candidate.line]
        occurrence_key = (section.section_id, candidate.kind, candidate.value)
        occurrence = occurrences[occurrence_key]
        occurrences[occurrence_key] += 1
        sentence = _sentence_for_line(parsed.lines, candidate.line, candidate.column)
        qualifier = candidate.qualifier
        if qualifier is None and candidate.kind == ClaimKind.cli_flag:
            qualifier = _qualifier(section.heading, sentence, section.heading_level)
        claims.append(
            Claim(
                claim_id=make_claim_id(
                    parsed.doc.path, section.heading_path, candidate.kind, candidate.value, occurrence
                ),
                kind=candidate.kind,
                raw_text=candidate.raw_text,
                value=candidate.value,
                qualifier=qualifier,
                expected_default=candidate.expected_default,
                doc_path=parsed.doc.path,
                line=candidate.line,
                column=candidate.column,
                section_id=section.section_id,
                extraction=candidate.extraction,
                extraction_confidence=candidate.confidence,
            )
        )
    return ExtractionResult(claims, skipped)

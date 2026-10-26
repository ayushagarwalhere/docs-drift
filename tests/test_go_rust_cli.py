"""Go and Rust CLI indexing: literal declarations only, with explicit negatives for every guessable shape."""

from pathlib import Path

import pytest

from docs_drift.config import Config
from docs_drift.index import build_index
from docs_drift.index.base import CodeIndex
from docs_drift.index.go_cli import registration
from docs_drift.index.native_common import cargo_crates, convert_case, kebab, rust_executable
from docs_drift.models import Symbol, SymbolKind, Verdict
from docs_drift.scanning import run_scan


def put(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def index_of(root: Path) -> CodeIndex:
    return build_index(root, Config(), no_cache=True)


def flags(index: CodeIndex) -> dict[str, Symbol]:
    return {symbol.name: symbol for symbol in index.symbols if symbol.kind == SymbolKind.cli_flag}


def scoped_flag(index: CodeIndex, name: str, scope: str) -> Symbol:
    return next(
        symbol
        for symbol in index.symbols
        if symbol.kind == SymbolKind.cli_flag and symbol.name == name and f".{scope}." in f".{symbol.qualified_name}."
    )


def commands(index: CodeIndex) -> dict[str, Symbol]:
    return {symbol.name: symbol for symbol in index.symbols if symbol.kind == SymbolKind.cli_command}


def envs(index: CodeIndex) -> dict[str, Symbol]:
    return {symbol.name: symbol for symbol in index.symbols if symbol.kind == SymbolKind.env_var}


# --------------------------------------------------------------------------- Go

GO_MOD = "module example.com/acme/tool\n\ngo 1.22\n"

COBRA_ROOT = """package root

import (
	"os"

	"github.com/spf13/cobra"
	"example.com/acme/tool/pkg/cmdutil"
)

const configFlag = "config"

func NewCmdRoot() *cobra.Command {
	cmd := &cobra.Command{
		Use:     "tool <command> [flags]",
		Version: "1.0.0",
	}
	cmd.PersistentFlags().StringVarP(&cfg, configFlag, "c", "tool.yaml", "Config file")
	cmd.PersistentFlags().BoolP("verbose", "v", false, "Verbose output")
	cmd.AddCommand(newListCmd())
	return cmd
}

func newListCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:     "list [pattern]",
		Aliases: []string{"ls"},
		Short:   "List items",
	}
	fs := cmd.Flags()
	fs.IntVarP(&limit, "limit", "L", 30, "Maximum number of items")
	fs.DurationVar(&timeout, "timeout", 5*time.Second, "Request timeout")
	fs.StringVar(&format, "format", "", "Output format (computed default)")
	fs.Int("zero", 0, "Zero means unset")
	fs.StringSliceVar(&labels, "label", nil, "Filter by label")
	cmdutil.StringEnumFlag(cmd, &state, "state", "s", "open", []string{"open", "closed"}, "Filter by state")
	cmdutil.NilBoolFlag(cmd, &draft, "draft", "d", "Only drafts")
	// cmd.Flags().String("ghost-comment", "", "commented out")
	_ = "cmd.Flags().String(\\"ghost-string\\", \\"\\", \\"in a string\\")"
	cmd.Flags().String(dynamicName(), "", "dynamic name is never guessed")
	_ = os.Getenv("TOOL_TOKEN")
	return cmd
}
"""

CMDUTIL = """package cmdutil

import "github.com/spf13/cobra"

func StringEnumFlag(cmd *cobra.Command, p *string, name, shorthand, defaultValue string, options []string,
	usage string) {
	cmd.Flags().VarP(newEnum(p, defaultValue, options), name, shorthand, usage)
}

func NilBoolFlag(cmd *cobra.Command, p **bool, name string, shorthand string, usage string) {
	cmd.Flags().VarPF(newNilBool(p), name, shorthand, usage)
}
"""

GO_MAIN = """package main

import "example.com/acme/tool/pkg/cmd/root"

func main() { root.NewCmdRoot().Execute() }
"""


def test_cobra_commands_flags_helpers_and_negatives(tmp_path: Path) -> None:
    put(tmp_path, "go.mod", GO_MOD)
    put(tmp_path, "cmd/tool/main.go", GO_MAIN)
    put(tmp_path, "pkg/cmd/root/root.go", COBRA_ROOT)
    put(tmp_path, "pkg/cmdutil/flags.go", CMDUTIL)
    put(tmp_path, "pkg/cmd/root/root_test.go", COBRA_ROOT.replace('"limit"', '"test-only"'))
    put(tmp_path, "testdata/fixture.go", COBRA_ROOT.replace('"limit"', '"fixture-only"'))
    index = index_of(tmp_path)
    found = flags(index)
    cmds = commands(index)

    assert cmds["tool"].parent_id is None and cmds["tool"].framework == "cobra"
    assert cmds["list"].aliases == ["ls"] and cmds["list"].parent_id == cmds["tool"].symbol_id
    assert {"help", "completion"} <= set(cmds)

    config = scoped_flag(index, "--config", "root")
    assert config.aliases == ["-c"] and config.default == "tool.yaml"
    assert scoped_flag(index, "--verbose", "root").aliases == ["-v"]
    assert scoped_flag(index, "--version", "root").derived
    limit = scoped_flag(index, "--limit", "list")
    assert limit.aliases == ["-L"] and limit.default == "30" and limit.line == 30
    # Expressions, empty strings and zero values are not documented defaults.
    assert found["--timeout"].default is None
    assert found["--format"].default is None
    assert found["--zero"].default is None
    assert found["--label"].default is None
    # Helpers that register a flag named by a parameter are resolved at their call sites.
    state = scoped_flag(index, "--state", "list")
    assert state.aliases == ["-s"] and state.file == "pkg/cmd/root/root.go"
    assert scoped_flag(index, "--draft", "list").aliases == ["-d"]
    # Comments, strings, dynamic names, tests and fixtures never become declarations.
    assert not {"--ghost-comment", "--ghost-string", "--test-only", "--fixture-only"} & set(found)
    assert all(symbol.file.endswith(".go") for symbol in found.values() if symbol.language == "go")
    assert not any("testdata" in symbol.file or symbol.file.endswith("_test.go") for symbol in index.symbols)
    assert envs(index)["TOOL_TOKEN"].language == "go"
    assert index.files_by_language["go"] == 5


def test_go_registration_method_shapes() -> None:
    assert registration("StringVarP") is not None and registration("StringVarP").default_index == 3  # type: ignore[union-attr]
    assert registration("BoolP").default_index == 2  # type: ignore[union-attr]
    assert registration("String").default_index == 1  # type: ignore[union-attr]
    assert registration("CountP").default_index is None  # type: ignore[union-attr]
    assert registration("VarPF").name_index == 1  # type: ignore[union-attr]
    assert registration("IP").short_index is None  # type: ignore[union-attr]
    for method in ("Changed", "Lookup", "GetString", "Parse", "Set", "P", "MarkHidden", "StringFoo"):
        assert registration(method) is None


def test_stdlib_flag_pflag_and_flagset_subcommands(tmp_path: Path) -> None:
    put(tmp_path, "go.mod", "module example.com/srv\n")
    put(
        tmp_path,
        "main.go",
        """package main

import (
	"flag"
	"os"
)

func main() {
	addr := flag.String("addr", ":8080", "listen address")
	flag.IntVar(&workers, "workers", 4, "worker count")
	serve := flag.NewFlagSet("serve", flag.ExitOnError)
	serve.Bool("tls", true, "enable TLS")
	x.String() // an unrelated String method is not a flag
	strings.NewReader("y").String()
	_ = os.LookupEnv("SRV_HOME")
}
""",
    )
    put(
        tmp_path,
        "tools/gen/main.go",
        """package main

import flag "github.com/spf13/pflag"

func main() { flag.StringP("out", "o", "gen.txt", "output") }
""",
    )
    put(
        tmp_path,
        ".github/tools/lint/main.go",
        'package main\nimport "flag"\nfunc main() { flag.Bool("repo-tooling", false, "") }\n',
    )
    put(
        tmp_path,
        "xtask/src/main.rs",
        "use clap::Parser;\n#[derive(Parser)]\nstruct X {\n    #[arg(long)]\n    xtask_only: bool,\n}\n",
    )
    index = index_of(tmp_path)
    found = flags(index)
    assert "-repo-tooling" not in found and "--xtask-only" not in found
    assert found["-addr"].aliases == ["--addr"] and found["-addr"].default == ":8080"
    assert found["-workers"].default == "4"
    assert scoped_flag(index, "-tls", "serve").default == "true"
    assert "-help" in found["-h"].aliases
    assert commands(index)["serve"].framework == "go-flag"
    assert found["--out"].aliases == ["-o"] and found["--out"].framework == "pflag"
    assert "SRV_HOME" in envs(index)
    assert not {name for name in found if name.lstrip("-") in {"y", "String"}}


URFAVE = """package main

import (
	"os"

	"github.com/urfave/cli/v2"
)

func main() {
	app := &cli.App{
		Name:    "greet",
		Version: "v1",
		Flags: []cli.Flag{
			&cli.StringFlag{
				Name: "config", Aliases: []string{"c"}, Value: "greet.yml", EnvVars: []string{"GREET_CONFIG"},
			},
			&cli.IntFlag{Name: "retries", Value: 3},
		},
		Commands: []*cli.Command{
			{
				Name:    "serve",
				Aliases: []string{"s"},
				Flags:   []cli.Flag{&cli.BoolFlag{Name: "watch", Aliases: []string{"w"}}},
			},
		},
	}
	app.Run(os.Args)
}
"""


def test_urfave_cli_v2_and_v3(tmp_path: Path) -> None:
    put(tmp_path, "go.mod", "module example.com/greet\n")
    put(tmp_path, "main.go", URFAVE)
    put(
        tmp_path,
        "cmd/v3tool/main.go",
        """package main

import "github.com/urfave/cli/v3"

var root = &cli.Command{
	Name: "v3tool",
	Flags: []cli.Flag{&cli.StringFlag{Name: "token", Sources: cli.EnvVars("V3_TOKEN", "LEGACY_TOKEN")}},
}
""",
    )
    index = index_of(tmp_path)
    cmds = commands(index)
    assert cmds["greet"].parent_id is None and cmds["serve"].parent_id == cmds["greet"].symbol_id
    assert cmds["serve"].aliases == ["s"]
    assert "h" in cmds["help"].aliases
    config = scoped_flag(index, "--config", "root")
    assert config.aliases == ["-c", "-config"] and config.default == "greet.yml"
    assert scoped_flag(index, "--retries", "root").default == "3"
    assert scoped_flag(index, "--watch", "serve").aliases == ["-w", "-watch"]
    assert scoped_flag(index, "--version", "root").aliases == ["-v"]
    assert {"GREET_CONFIG", "V3_TOKEN", "LEGACY_TOKEN"} <= set(envs(index))
    assert cmds["v3tool"].parent_id is None


def test_go_environment_sources_and_dynamic_prefixes(tmp_path: Path) -> None:
    put(tmp_path, "go.mod", "module example.com/envs\n")
    put(
        tmp_path,
        "config.go",
        """package config

import (
	"os"
	"strings"

	"github.com/spf13/viper"
)

const tokenVar = "ENVS_TOKEN"

type Settings struct {
	Home   string `env:"ENVS_HOME,required" json:"home"`
	Skip   string `env:"-"`
	Nested Nested `envPrefix:"ENVS_DB_"`
}

func load() {
	_ = os.Getenv(tokenVar)
	viper.BindEnv("port", "ENVS_PORT", "PORT")
	viper.SetEnvPrefix("envs")
	viper.AutomaticEnv()
	for _, key := range []string{"ENVS_A_ONE", "ENVS_B_TWO", "lowercase"} {
		_ = os.Getenv(key)
	}
}

func overrides(environ []string) {
	const prefix = "ENVSX"
	for _, kv := range environ {
		if strings.HasPrefix(kv, prefix) {
		}
	}
}
""",
    )
    index = index_of(tmp_path)
    found = envs(index)
    assert {"ENVS_TOKEN", "ENVS_HOME", "ENVS_PORT", "PORT", "ENVS_A_ONE", "ENVS_B_TWO"} <= set(found)
    assert found["ENVS_A_ONE"].derived and "lowercase" not in found and "-" not in found
    prefixes = {symbol.name for symbol in found.values() if symbol.partial_prefix}
    assert prefixes == {"ENVS_*", "ENVS_DB_*", "ENVSX*"}
    assert all(not found[name].authoritative for name in prefixes)


def test_viper_automatic_env_without_prefix_covers_everything(tmp_path: Path) -> None:
    put(tmp_path, "go.mod", "module example.com/v\n")
    put(tmp_path, "main.go", 'package main\nimport "github.com/spf13/viper"\nfunc main() { viper.AutomaticEnv() }\n')
    assert {symbol.name for symbol in index_of(tmp_path).symbols if symbol.partial_prefix} == {"*"}


def test_simplecobra_named_commands(tmp_path: Path) -> None:
    put(tmp_path, "go.mod", "module github.com/acme/site\n")
    put(
        tmp_path,
        "commands/server.go",
        """package commands

import "github.com/bep/simplecobra"

type serverCommand struct{ port int }

func (c *serverCommand) Name() string { return "server" }

func (c *serverCommand) Init(cd *simplecobra.Commandeer) error {
	cmd := cd.CobraCommand
	cmd.Aliases = []string{"serve"}
	cmd.Flags().IntVarP(&c.port, "port", "p", 1313, "port")
	return nil
}

type other struct{}

func (o other) Name() string { return "not-a-command" }
""",
    )
    index = index_of(tmp_path)
    cmds = commands(index)
    assert cmds["server"].aliases == ["serve"] and "not-a-command" not in cmds
    port = scoped_flag(index, "--port", "server")
    assert port.aliases == ["-p"] and port.default == "1313"


def test_go_syntax_errors_do_not_leak_guesses(tmp_path: Path) -> None:
    put(tmp_path, "go.mod", "module example.com/broken\n")
    put(
        tmp_path,
        "main.go",
        """package main

import "github.com/spf13/cobra"

func ok() { cmd := &cobra.Command{Use: "fine"}; cmd.Flags().Bool("good", false, "") }

var x = := cmd.Flags().Bool("inside-error", false, "") :=
""",
    )
    found = flags(index_of(tmp_path))
    assert "--good" in found and "--inside-error" not in found


# ------------------------------------------------------------------------- Rust

CARGO = """[package]
name = "fd-find"
version = "1.0.0"

[[bin]]
name = "fd"
path = "src/main.rs"
"""

CLAP_DERIVE = """use clap::{Args, Parser, Subcommand};

/// Not a declaration: #[arg(long = "doc-comment")]
#[derive(Parser, Debug)]
#[command(name = "fd", version, about = "find things")]
pub struct Opts {
    /// Include hidden files
    #[arg(long, short = 'H', alias = "all", visible_aliases = ["include-hidden"], env = "FD_HIDDEN")]
    pub hidden: bool,

    #[arg(long = "no-ignore", short = 'I', short_alias = 'u')]
    pub no_ignore: bool,

    #[arg(short, long, default_value_t = 8)]
    pub threads: usize,

    #[arg(long, env, default_value = "auto")]
    pub color_mode: String,

    #[arg(long, default_value_t = Duration::from_secs(3))]
    pub timeout: Duration,

    /// Positional pattern
    pub pattern: Option<String>,

    #[arg(long = "ghost", hide = true)]
    #[cfg(any())]
    pub ghost: (),

    #[command(flatten)]
    pub output: OutputArgs,

    #[command(subcommand)]
    pub command: Option<Commands>,
}

#[derive(Args, Debug)]
pub struct OutputArgs {
    #[arg(long)]
    pub json: bool,
}

#[derive(Subcommand, Debug)]
pub enum Commands {
    /// Rebuild the cache
    #[command(alias = "rb")]
    RebuildCache { #[arg(long)] force: bool },
    #[command(name = "ls", visible_alias = "list")]
    ListAll(ListArgs),
    #[command(external_subcommand)]
    External(Vec<String>),
}

#[derive(Args, Debug)]
pub struct ListArgs {
    #[arg(short = 'l', long)]
    pub long_format: bool,
}

fn main() {
    let _ = Opts::parse();
    let _ = std::process::Command::new("ls").arg("-la");
    let _ = "Arg::new(\\"x\\").long(\\"in-a-string\\")";
}

#[cfg(test)]
mod tests {
    use clap::Parser;
    #[derive(Parser)]
    struct TestOnly {
        #[arg(long)]
        test_only_flag: bool,
    }
}
"""


def test_clap_derive_names_defaults_aliases_and_scopes(tmp_path: Path) -> None:
    put(tmp_path, "Cargo.toml", CARGO)
    put(tmp_path, "src/main.rs", CLAP_DERIVE)
    index = index_of(tmp_path)
    found = flags(index)
    cmds = commands(index)
    assert cmds["fd"].parent_id is None and cmds["fd"].aliases == []
    hidden = scoped_flag(index, "--hidden", "root")
    assert hidden.aliases == ["-H", "--all", "--include-hidden"]
    assert found["--no-ignore"].aliases == ["-I", "-u"]
    threads = found["--threads"]
    assert threads.aliases == ["-t"] and threads.default == "8"
    assert found["--color-mode"].default == "auto"
    assert found["--timeout"].default is None
    assert {"--version", "--help"} <= set(found) and found["--version"].aliases == ["-V"]
    assert scoped_flag(index, "--json", "root").language == "rust"
    # Subcommands: kebab-case variants, explicit names, aliases and scoped arguments.
    assert cmds["rebuild-cache"].aliases == ["rb"] and cmds["rebuild-cache"].parent_id == cmds["fd"].symbol_id
    assert cmds["ls"].aliases == ["list"]
    assert scoped_flag(index, "--force", "rebuild-cache")
    assert scoped_flag(index, "--long-format", "ls").aliases == ["-l"]
    assert "external" not in cmds
    environment = envs(index)
    assert {"FD_HIDDEN", "COLOR_MODE"} <= set(environment)
    # Doc comments, strings, std::process::Command, cfg(test) modules and positionals stay out.
    assert not {"--doc-comment", "--in-a-string", "--test-only-flag", "--pattern"} & set(found)
    ls = [symbol for symbol in index.symbols if symbol.kind == SymbolKind.cli_command and symbol.name == "ls"]
    assert len(ls) == 1 and ls[0].framework == "clap"


def test_clap_rename_all_and_unknown_styles(tmp_path: Path) -> None:
    put(tmp_path, "Cargo.toml", '[package]\nname = "tool"\n')
    put(
        tmp_path,
        "src/main.rs",
        """use clap::Parser;
#[derive(Parser)]
#[command(rename_all = "snake_case")]
struct Opts {
    #[arg(long)]
    max_depth: u8,
}
#[derive(Parser)]
#[command(rename_all = "made_up")]
struct Other {
    #[arg(long)]
    weird_name: u8,
    #[arg(long = "explicit-name")]
    weird_explicit: u8,
}
fn main() { Opts::parse(); }
""",
    )
    found = flags(index_of(tmp_path))
    assert "--max_depth" in found and "--max-depth" not in found
    assert "--explicit-name" in found and not {"--weird-name", "--weird_name"} & set(found)


def test_clap_builder_chains_bindings_and_cargo_names(tmp_path: Path) -> None:
    put(tmp_path, "Cargo.toml", '[package]\nname = "bat"\n')
    put(
        tmp_path,
        "src/bin/bat/clap_app.rs",
        """use clap::{
    builder::styling::{AnsiColor, Effects},
    crate_name, Arg, ArgAction, Command,
};

pub fn build_app() -> Command {
    let mut app = Command::new(crate_name!())
        .version("1")
        .arg(Arg::new("language").long("language").short('l').alias("lang").env("BAT_LANG"));
    app = app.arg(Arg::new("tabs").long("tabs").default_value("4"));
    app = app.arg(Arg::new("FILE").help("positional only"));
    let name = "dynamic";
    app = app.arg(Arg::new(name).long(name));
    app.subcommand(cache_cmd())
}

fn cache_cmd() -> Command {
    Command::new("cache")
        .visible_alias("c")
        .arg(Arg::new("clear").long("clear").short('c').action(ArgAction::SetTrue))
        .arg(clap::arg!(-b --build "Build the cache"))
}
""",
    )
    put(tmp_path, "src/bin/bat/main.rs", "fn main() {}\n")
    index = index_of(tmp_path)
    found = flags(index)
    cmds = commands(index)
    assert cmds["bat"].parent_id is None
    assert cmds["cache"].aliases == ["c"] and cmds["cache"].parent_id == cmds["bat"].symbol_id
    language = scoped_flag(index, "--language", "root")
    assert language.aliases == ["-l", "--lang"]
    assert scoped_flag(index, "--tabs", "root").default == "4"
    assert scoped_flag(index, "--clear", "cache").aliases == ["-c"]
    assert scoped_flag(index, "--build", "cache").aliases == ["-b"]
    assert scoped_flag(index, "--version", "root").aliases == ["-V"]
    assert "BAT_LANG" in envs(index)
    assert "--dynamic" not in found and "--FILE" not in found


def test_structopt_and_argh_basics(tmp_path: Path) -> None:
    put(tmp_path, "Cargo.toml", '[package]\nname = "old"\n')
    put(
        tmp_path,
        "src/main.rs",
        """use structopt::StructOpt;
#[derive(StructOpt)]
#[structopt(name = "old")]
struct Opt {
    #[structopt(short, long)]
    debug: bool,
    #[structopt(long = "out-dir", default_value = "target")]
    out: String,
}
fn main() { Opt::from_args(); }
""",
    )
    put(
        tmp_path,
        "src/argh_cli.rs",
        """use argh::FromArgs;
#[derive(FromArgs)]
#[argh(subcommand, name = "two")]
struct SubTwo {
    #[argh(switch, short = 'f')]
    fooey: bool,
    #[argh(option)]
    max_items: usize,
    #[argh(positional)]
    file: String,
}
""",
    )
    index = index_of(tmp_path)
    found = flags(index)
    assert found["--debug"].aliases == ["-d"] and found["--out-dir"].default == "target"
    assert commands(index)["two"].framework == "argh"
    assert scoped_flag(index, "--fooey", "two").aliases == ["-f"]
    assert "--max-items" in found and "--file" not in found


def test_rust_environment_sources(tmp_path: Path) -> None:
    put(tmp_path, "Cargo.toml", '[package]\nname = "envy-app"\n')
    put(
        tmp_path,
        "src/main.rs",
        """use std::env;
const HOME_VAR: &str = "APP_HOME";
pub mod env_names {}
pub mod env {
    pub const APP_THEME: &str = "APP_THEME";
}
fn main() {
    let _ = env::var("APP_TOKEN");
    let _ = std::env::var_os("APP_CACHE");
    let _ = env::var(HOME_VAR);
    let _ = option_env!("APP_BUILD_PREFIX");
    let _ = env!("CARGO_PKG_VERSION");
    let _: Config = envy::prefixed("APP_CFG_").from_env().unwrap();
    for (flag, key) in [("--tabs", "APP_TABS"), ("--width", "APP_WIDTH")] {
        let _ = env::var(key);
    }
    let _ = "APP_NOT_READ";
}
""",
    )
    found = envs(index_of(tmp_path))
    assert {"APP_TOKEN", "APP_CACHE", "APP_HOME", "APP_BUILD_PREFIX", "APP_THEME", "APP_TABS", "APP_WIDTH"} <= set(
        found
    )
    assert "CARGO_PKG_VERSION" not in found and "APP_NOT_READ" not in found
    assert found["APP_CFG_*"].partial_prefix


def test_clap_computed_attribute_values_are_not_guessed(tmp_path: Path) -> None:
    put(tmp_path, "Cargo.toml", '[package]\nname = "uv"\n')
    put(
        tmp_path,
        "src/main.rs",
        """use clap::{Parser, Subcommand};
#[derive(Parser)]
#[command(name = "uv", version)]
struct Cli {
    #[arg(long, env = EnvVars::UV_CACHE_DIR)]
    cache_dir: Option<String>,
    #[arg(long = FLAG_NAME)]
    computed_long: bool,
    #[arg(long, env = some_function())]
    offline: bool,
    #[command(subcommand)]
    command: Commands,
}
#[derive(Subcommand)]
enum Commands {
    #[command(flatten)]
    Project(Box<ProjectCommand>),
    #[command(name = COMPUTED)]
    Hidden,
    Venv,
}
#[derive(Subcommand)]
enum ProjectCommand {
    Run,
    Sync,
}
pub struct EnvVars;
impl EnvVars {
    pub const UV_NO_CACHE: &'static str = "UV_NO_CACHE";
    pub const NOT_ENV_LIKE: &'static str = "lowercase";
}
fn main() { Cli::parse(); }
""",
    )
    index = index_of(tmp_path)
    found = flags(index)
    cmds = commands(index)
    environment = envs(index)
    assert "--cache-dir" in found and "UV_CACHE_DIR" in environment and "CACHE_DIR" not in environment
    assert "--offline" in found and "OFFLINE" not in environment
    assert "--computed-long" not in found
    assert {"UV_NO_CACHE"} <= set(environment) and "lowercase" not in environment
    assert cmds["run"].parent_id == cmds["uv"].symbol_id and cmds["sync"].parent_id == cmds["uv"].symbol_id
    assert cmds["venv"].parent_id == cmds["uv"].symbol_id
    assert "hidden" not in cmds and "project" not in cmds


def test_subcommands_declared_elsewhere_are_never_roots(tmp_path: Path) -> None:
    put(tmp_path, "go.mod", "module example.com/acme/tool\n")
    put(
        tmp_path,
        "pkg/cmd/install/install.go",
        """package install

import "github.com/spf13/cobra"

func NewCmdInstall() *cobra.Command {
	return &cobra.Command{Use: "install <package>"}
}
""",
    )
    put(tmp_path, "Cargo.toml", '[package]\nname = "tool"\n')
    put(
        tmp_path,
        "src/commands.rs",
        """use clap::Subcommand;
#[derive(Subcommand)]
pub enum PipCommand { Install, Uninstall }
""",
    )
    index = index_of(tmp_path)
    install = [symbol for symbol in index.symbols if symbol.kind == SymbolKind.cli_command and symbol.name == "install"]
    assert len(install) == 2
    assert all(symbol.parent_id is not None for symbol in install)
    assert commands(index)["uninstall"].parent_id is not None


def test_pest_parser_derive_and_process_command_are_not_clap(tmp_path: Path) -> None:
    put(tmp_path, "Cargo.toml", '[package]\nname = "calc"\n')
    put(
        tmp_path,
        "src/main.rs",
        """use pest_derive::Parser;
use std::process::Command;
#[derive(Parser)]
#[grammar = "calc.pest"]
struct CalcParser;
fn main() { Command::new("git").arg("status"); }
""",
    )
    index = index_of(tmp_path)
    assert not [symbol for symbol in index.symbols if symbol.kind in {SymbolKind.cli_flag, SymbolKind.cli_command}]


def test_cargo_executable_resolution(tmp_path: Path) -> None:
    put(tmp_path, "Cargo.toml", '[workspace]\nmembers = ["crates/*"]\n')
    put(tmp_path, "crates/core/Cargo.toml", '[package]\nname = "ripgrep"\n[[bin]]\nname = "rg"\npath = "main.rs"\n')
    put(tmp_path, "crates/core/main.rs", "fn main() {}\n")
    put(tmp_path, "crates/multi/Cargo.toml", '[package]\nname = "multi"\n')
    put(tmp_path, "crates/multi/src/bin/alpha.rs", "fn main() {}\n")
    put(tmp_path, "crates/multi/src/bin/beta/main.rs", "fn main() {}\n")
    put(tmp_path, "crates/lib/Cargo.toml", '[package]\nname = "lib"\n')
    crates = cargo_crates(tmp_path)
    assert rust_executable("crates/core/main.rs", crates) == ("rg", ["ripgrep"])
    assert rust_executable("crates/core/flags.rs", crates) == ("rg", ["ripgrep"])
    assert rust_executable("crates/multi/src/bin/beta/main.rs", crates)[0] == "beta"
    assert rust_executable("crates/multi/src/shared.rs", crates) == (None, [])
    assert rust_executable("crates/lib/src/lib.rs", crates) == (None, [])


@pytest.mark.parametrize(
    ("name", "style", "expected"),
    [
        ("max_depth", None, "max-depth"),
        ("RebuildCache", None, "rebuild-cache"),
        ("HTTPServer", "kebab-case", "http-server"),
        ("max_depth", "SCREAMING_SNAKE_CASE", "MAX_DEPTH"),
        ("max_depth", "camelCase", "maxDepth"),
        ("max_depth", "PascalCase", "MaxDepth"),
        ("max_depth", "lower", "maxdepth"),
        ("max_depth", "no-such-style", None),
    ],
)
def test_case_conversion(name: str, style: str | None, expected: str | None) -> None:
    assert convert_case(name, style) == expected
    assert kebab("RebuildCache") == "rebuild-cache"


# ------------------------------------------------------------------ End to end


def _findings(repo: Path) -> dict[str, tuple[Verdict, float, str | None]]:
    """Findings keyed by claim value; default claims are keyed ``default:<subject>``."""
    result = run_scan(repo, Config(), history=False)
    return {
        ("default:" if finding.claim.kind.value == "default_value" else "") + finding.claim.value: (
            finding.verdict,
            finding.confidence,
            finding.suggestion,
        )
        for finding in result.findings
        if finding.claim.kind.value in {"cli_flag", "cli_command", "env_var", "default_value"}
    }


def test_go_docs_to_findings(tmp_path: Path) -> None:
    put(tmp_path, "go.mod", "module example.com/acme/tool\n")
    put(
        tmp_path,
        "main.go",
        """package main

import (
	"os"

	"github.com/spf13/cobra"
	"github.com/spf13/viper"
)

func main() {
	root := &cobra.Command{Use: "tool"}
	root.Flags().IntVar(&workers, "max-workers", 4, "worker count")
	root.Flags().Bool("dry-run", false, "print only")
	_ = os.Getenv("TOOL_TOKEN")
	viper.SetEnvPrefix("tool")
	viper.AutomaticEnv()
	root.Execute()
}
""",
    )
    put(
        tmp_path,
        "README.md",
        "# Tool\n\n"
        "Use `--max-workers` to bound concurrency (default: 16).\n\n"
        "Pass `--dry_run` to preview changes.\n\n"
        "Set `TOOL_TOKEN` to authenticate. `TOOL_TIMEOUT` is read through the config prefix.\n",
    )
    findings = _findings(tmp_path)
    assert findings["--max-workers"][0] == Verdict.ok
    renamed = findings["--dry_run"]
    assert renamed[0] == Verdict.renamed and renamed[2] == "--dry-run" and renamed[1] >= Config().min_confidence
    assert findings["TOOL_TOKEN"][0] == Verdict.ok
    assert findings["TOOL_TIMEOUT"][0] == Verdict.unverifiable
    assert findings["default:--max-workers"][0] == Verdict.default_changed
    assert findings["default:--max-workers"][2] == "4"


def test_rust_docs_to_findings(tmp_path: Path) -> None:
    put(tmp_path, "Cargo.toml", '[package]\nname = "finder"\n')
    put(
        tmp_path,
        "src/main.rs",
        """use clap::Parser;
#[derive(Parser)]
#[command(version)]
struct Opts {
    #[arg(long, short = 'H')]
    hidden: bool,
    #[arg(long = "ignore-case", short = 'i', env = "FINDER_IGNORE_CASE")]
    case_insensitive: bool,
}
fn main() { Opts::parse(); }
""",
    )
    put(
        tmp_path,
        "README.md",
        "# finder\n\nUse `--hidden` (or `-H`) to include dotfiles.\n\n"
        "Pass `--ignore_case` for case-insensitive search, or set `FINDER_IGNORE_CASE`.\n",
    )
    findings = _findings(tmp_path)
    assert findings["--hidden"][0] == Verdict.ok
    assert findings["-H"][0] == Verdict.ok
    assert findings["FINDER_IGNORE_CASE"][0] == Verdict.ok
    assert findings["--ignore_case"][0] == Verdict.renamed and findings["--ignore_case"][2] == "--ignore-case"


def test_unsupported_go_flag_parsers_stay_below_the_reporting_threshold(tmp_path: Path) -> None:
    put(tmp_path, "go.mod", "module example.com/custom\n")
    put(
        tmp_path, "main.go", 'package main\nfunc main() { parse(os.Args) }\nfunc parse(a []string) { _ = "--layout" }\n'
    )
    put(tmp_path, "README.md", "# custom\n\nUse `--layout=reverse` to flip the list.\n")
    findings = _findings(tmp_path)
    verdict, confidence, _ = findings["--layout"]
    assert verdict in {Verdict.missing, Verdict.unverifiable} and confidence < Config().min_confidence

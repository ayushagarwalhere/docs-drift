# Go and Rust CLIs

<!-- docs-drift-ignore-file: every flag, variable and path below is an example of Go/Rust source elsewhere. -->

docs-drift indexes the command-line surface of Go and Rust programs: flags,
subcommands, their aliases, literal defaults and the environment variables the
program reads. Documentation that mentions `--flag`, a subcommand, or `APP_TOKEN`
can then be matched against the declaration that supports it, with the source
line as evidence.

Only this surface is covered. Go and Rust functions, types, routes and
configuration keys are still not indexed, so claims about them remain
`unverifiable` rather than `missing`.

## What is recognised

Every name must be a literal in source. A computed name is skipped, not guessed.

### Go

| Framework | Declarations |
| --- | --- |
| Cobra | `&cobra.Command{Use: "list [flags]", Aliases: []string{"ls"}}`; `cmd.Flags()` / `cmd.PersistentFlags()` registrations (`String`, `StringP`, `StringVar`, `StringVarP`, `Bool…`, `Int…`, `Duration…`, `StringSlice…`, `Count…`, `Var`, `VarP`, …); `parent.AddCommand(child)` within a file; `Execute()` marks the root |
| spf13/pflag | `pflag.StringP(...)`, `pflag.NewFlagSet(...)` and FlagSet variables |
| `flag` (standard library) | `flag.String("addr", ":8080", "")` is indexed as `-addr` with alias `--addr`; `flag.NewFlagSet("serve", …)` is a `serve` subcommand |
| urfave/cli v1–v3 | `cli.App` / `cli.Command{Name, Aliases, Commands/Subcommands}` (including elided `[]*cli.Command{{...}}` elements); `cli.StringFlag{Name, Aliases, Value, EnvVars}`, v3 `Sources: cli.EnvVars(...)`, v1 `EnvVar: "A,B"` and `Name: "config, c"` |
| bep/simplecobra | `Name() string { return "server" }` on a type whose `Init(*simplecobra.Commandeer)` registers flags |

Repository helper functions that register a flag named by one of their
parameters, such as `StringEnumFlag(cmd, &v, "state", "s", ...)`, are found once
per repository and their call sites are indexed with the literal names passed.

Implicit declarations are added as derived symbols: `--help`/`-h` on every Cobra,
urfave and clap command, `--version` when a version is configured, Cobra's
`help` and `completion` subcommands, and the standard library's `-h`/`-help`.

### Rust

| Framework | Declarations |
| --- | --- |
| clap derive (v3/v4) | `#[derive(Parser)]`, `#[derive(Args)]`, `#[derive(Subcommand)]`; `#[arg(...)]` / `#[clap(...)]` with `long`, `short`, `alias(es)`, `visible_alias(es)`, `short_alias(es)`, `env`, `default_value`, `default_value_t`; `#[command(name, alias, version, rename_all, subcommand, flatten)]` |
| clap builder | `Command::new("x")` / `App::new` / `SubCommand::with_name` chains with `.alias`, `.subcommand`, `.arg(Arg::new("x").long("x").short('x').env("X").default_value("v"))`, `arg!(-c --config <FILE> "…")`, and `let mut app = …; app = app.arg(…)` bindings |
| structopt | `#[derive(StructOpt)]` with `#[structopt(short, long, name, default_value, env)]` |
| argh | `#[derive(FromArgs)]` with `#[argh(switch / option, short, long)]` and `#[argh(subcommand, name = "x")]` |

Derived names follow clap's rules: a field without an explicit `long` value
becomes kebab-case (`max_depth` → `--max-depth`), `short` without a value uses
the first character, `env` without a value uses SCREAMING_SNAKE_CASE, and
subcommand variants become kebab-case (`RebuildCache` → `rebuild-cache`). An
unknown `rename_all` style makes derived names unknown; explicit names still
count. A computed value (`long = NAME`, `#[command(name = NAME)]`) is never turned
into a name; that flag or subcommand is left out. `Command::new` is only treated as clap when the file imports clap's
`Command`, so `std::process::Command::new("ls")` is never a subcommand, and a
pest `#[derive(Parser)]` with `#[grammar]` is ignored.

### Environment variables

| Source | Go | Rust |
| --- | --- | --- |
| Direct reads | `os.Getenv("X")`, `os.LookupEnv("X")` (constants in the same file are resolved) | `std::env::var("X")`, `env::var_os("X")`, `env!("X")`, `option_env!("X")` (same-file `const` resolved) |
| Framework bindings | urfave `EnvVars`, viper `BindEnv("key", "X")`, `env:"X"` struct tags | clap `env = "X"` / `.env("X")`, `env` without a value, `env = EnvVars::APP_X` (self-named constant) |
| Literal tables | `for _, k := range []string{"APP_A"} { os.Getenv(k) }` | `for (_, k) in [("--a", "APP_A")] { env::var(k) }`, iterator chains, `mod env { const APP_A: &str = "APP_A"; }` and `impl EnvVars { const ... }` registries |

Dynamic prefixes are recorded as partial-prefix symbols named `PREFIX*`: viper
`SetEnvPrefix("app")` (→ `APP_*`) and `AutomaticEnv()` without a prefix (→ every
name), `envPrefix:"APP_"` tags, `strings.HasPrefix(key, "APP")` while scanning
`os.Environ()`, `envy::prefixed("APP_")`, figment `Env::prefixed` and config-rs
`Environment::with_prefix`. A documented variable under such a prefix is
reported `unverifiable`, never `missing`, and a prefix is never suggested as a
rename.

## Executables

The root command is the one named after an executable of the repository:

- Go: `go build` names a program after its directory, so every directory with
  a `package main` file contributes its name, and the module root contributes the
  last element of the `go.mod` module path (major-version suffixes removed). A
  Cobra command whose `Use` starts with that name, or whose `Execute()` is
  called, is the root.
- Rust: the nearest Cargo manifest provides `[[bin]]` names, the package name for
  the default main.rs binary, and one name per file or directory under src/bin. A `#[derive(Parser)]`
  struct that is parsed (`Opts::parse()`) or names itself with
  `#[command(name)]`/`version`, and `Command::new(crate_name!())`, is the root.

Root flags are scoped as `root`, so a flag documented under a subcommand that
is declared on the root is still matched as inherited.

## Defaults

Defaults are recorded only when they are unambiguous literals of the flag's own
type: Cobra/pflag/flag default arguments and urfave `Value` for string, bool,
integer and float flags, and clap `default_value = "…"` / `default_value_t =
literal`. Expressions (`5*time.Second`, `Duration::from_secs(3)`), slices,
empty strings and zero values are not recorded, because they usually mean "computed
elsewhere". Duplicate declarations with different defaults make a default claim
unverifiable.

## What is skipped

- Files under `testdata/`, `tests/`, `test/`, `examples/`, `example/`,
  `_examples/`, `benches/`, `fuzz/`, `vendor/`, `third_party/`, `target/` and
  `xtask/`, dot-directories such as `.github/`, Go `_test.go` files, and Rust
  `#[cfg(test)]` modules and `#[test]` functions.
- Declarations inside tree-sitter error-recovery regions.
- Comments and string contents: only real call and attribute syntax is read.
- Flag parsers that are not listed above (for example ripgrep's and fzf's
  hand-written parsers). Their documented flags stay below the reporting
  threshold instead of being reported against an incomplete index.
- Whole shell commands. Shell blocks become checked commands only for
  executables named by Python or JavaScript packaging metadata, so `fd -HI x`
  in a fenced block is not yet validated token by token. Flags, subcommands and
  environment variables written as inline code are checked.

## Coverage and confidence

`cli_flag`, `cli_command` and `env_var` claims are `partial` for Go and Rust; all
other claim kinds stay `none`. A documented flag that is not indexed is
`missing` at confidence 0.5, which is below the default `min_confidence` of 0.6,
unless one indexed name is a clear rename. In repositories with several source
languages the engine takes the most conservative coverage, so such claims are
often `unverifiable`.

Helper functions are discovered across the whole module; if a helper changes
while its callers do not, cached call-site symbols are refreshed on the next
uncached scan (`docs-drift cache clear` or a new docs-drift version).

; Rust CLI shapes read by index/rust_cli.py. One query pass per file collects every
; node the extractor inspects; Python then reads only literal arguments.

; #[derive(Parser)] structs, #[derive(Subcommand)] enums and their attributes.
(struct_item) @struct
(enum_item) @enum

; Test-only code (#[cfg(test)] modules, #[test] functions) is excluded by range.
(mod_item) @scope
(function_item) @function
(impl_item) @scope

; Builder constructors and environment reads.
((call_expression function: (scoped_identifier) @_path) @constructor
 (#match? @_path "(^|::)(Command|App|SubCommand|Arg)::(new|with_name)$"))
((call_expression function: [(scoped_identifier) (identifier)] @_path) @env_call
 (#match? @_path "((^|::)env::var(_os)?|^var(_os)?|envy::prefixed|Env::prefixed|Environment::with_prefix)$"))

; Builder methods that describe a command (args are read from Arg::new chains directly).
((call_expression function: (field_expression field: (field_identifier) @_method)) @command_method
 (#any-of? @_method
   "alias" "aliases" "visible_alias" "visible_aliases" "version" "long_version"
   "disable_help_flag" "disable_version_flag" "subcommand" "subcommands"))

; String constants (resolved for env::var(NAME) and `mod env { const ... }`).
(const_item) @const

; arg!(...), command!(), env!("X"), option_env!("X").
(macro_invocation) @macro

; Bindings used to follow `let mut app = Command::new(..); app = app.arg(..)`.
(let_declaration) @binding
(assignment_expression) @binding

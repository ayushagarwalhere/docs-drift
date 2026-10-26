; Go CLI shapes read by index/go_cli.py. One query pass per file collects every
; node the extractor inspects; Python then reads only literal arguments.

(import_spec) @import
(const_spec) @spec
(var_spec) @spec
(short_var_declaration) @bind
(assignment_statement) @bind
(function_declaration) @function
(method_declaration) @function

; &cobra.Command{Use: "x"}, &cli.StringFlag{Name: "x"}, cli.App{...}
(composite_literal type: (qualified_type)) @composite
(composite_literal type: (generic_type)) @composite
; []*cli.Command{{Name: "serve"}} elides the element type.
(composite_literal type: (slice_type)) @slice

; cmd.Flags().StringVarP(&v, "name", "n", "default", "usage") and friends.
((call_expression
   function: (selector_expression field: (field_identifier) @_method)) @registration
 (#match? @_method "^(Bool|String|Int|Int8|Int16|Int32|Int64|Uint|Uint8|Uint16|Uint32|Uint64|Float32|Float64|Duration|Count|IP|IPMask|IPNet|IPSlice|BytesHex|BytesBase64|StringSlice|StringArray|IntSlice|Int32Slice|Int64Slice|UintSlice|Float32Slice|Float64Slice|BoolSlice|DurationSlice|StringToString|StringToInt|StringToInt64|Func|BoolFunc|Text)?(Var)?(P)?(F)?$"))

; Command wiring, execution and environment access.
((call_expression
   function: (selector_expression field: (field_identifier) @_name)) @call
 (#any-of? @_name
   "AddCommand" "Execute" "ExecuteC" "ExecuteContext" "ExecuteContextC" "Run" "RunContext"
   "NewFlagSet" "Getenv" "LookupEnv" "BindEnv" "SetEnvPrefix" "AutomaticEnv"
   "HasPrefix" "CutPrefix" "TrimPrefix"))

; struct fields with tags (env:"NAME", envPrefix:"APP_").
(field_declaration tag: (_)) @tagged

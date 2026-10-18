; Query examples used by the JS/TS indexer; extraction is performed on these AST shapes.
(function_declaration name: (identifier) @function.name) @function
(lexical_declaration (variable_declarator name: (identifier) @arrow.name value: (arrow_function))) @arrow
(class_declaration name: (type_identifier) @class.name) @class

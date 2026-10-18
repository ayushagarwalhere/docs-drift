# Indexing limitations

Unsupported or incomplete surfaces are reported as unverifiable; absence from a partial index is not proof of drift.

- **Dynamic registration and plugins.** A CLI assembled from plugin discovery or loops, such as `for plugin in plugins: app.add_command(plugin.command)`, cannot be resolved statically. Its undocumented flags may be unverifiable.
- **Cross-module router composition.** Prefixes built from runtime settings or complex imported router graphs may be only partially resolved. A route claim may remain unverifiable until prefixes are literal.
- **Generated framework declarations.** Spring Boot and deeply dynamic oclif/yargs declarations are not fully indexed. Their command and route claims remain unverifiable rather than being called missing.
- **Frameworks not implemented yet.** Django/DRF, Next.js, NestJS, OpenAPI, Docker Compose, Helm, Rust and Go surfaces are absent or partial. Their claims must stay unverifiable. Dependency-based unsupported-framework discovery is not wired into the CLI yet.
- **Large indexes and dependency edges.** The cache is incremental per file, but import edges are currently empty and indexing is sequential. Prefix results are not yet recomputed from cached cross-file edges, and there is no process pool for repositories with more than 200 files.

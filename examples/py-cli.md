# Documentation health: 56/100

| Verdict | Count |
| --- | ---: |
| ok | 5 |
| missing | 1 |
| renamed | 2 |
| default_changed | 1 |
| invalid_command | 0 |
| unverifiable | 1 |

## README.md

**unverifiable · line 4** (default 8000)

```diff
- (default 8000)
+ No authoritative indexed default was available for this subject.
```

<details><summary>Evidence</summary>

README.md:4

```text
(default 8000)
```

450a93d Rename port to listen 2026-10-02T09:00:00+00:00
</details>

**renamed · line 4** --port

```diff
- --port
+ --listen
```

<details><summary>Evidence</summary>

README.md:4

```text
--port
```

cli.py:1

```text
-    serve.add_argument("--port", type=int, default=8000)
+    serve.add_argument("--listen", type=int, default=8000)
```

450a93d Rename port to listen 2026-10-02T09:00:00+00:00
</details>

**default_changed · line 6** default is &#96;4&#96;

```diff
- default is `4`
+ 8
```

<details><summary>Evidence</summary>

README.md:6

```text
default is `4`
```

cli.py:10

```text
cli.serve.--workers default=8
```

cli.py:10

```text
-    serve.add_argument("--workers", type=int, default=4)
+    serve.add_argument("--workers", type=int, default=8)
```

397bff7 Increase the worker default 2026-10-03T09:00:00+00:00
</details>

**missing · line 7** APP_DEBUG

```diff
- APP_DEBUG
+ APP_LOG_LEVEL
```

<details><summary>Evidence</summary>

README.md:7

```text
APP_DEBUG
```

cli.py:1

```text
-    debug = os.environ.get("APP_DEBUG", "0")
-    return parser.parse_args(), debug
+    log_level = os.environ.get("APP_LOG_LEVEL", "INFO")
+    return parser.parse_args(), log_level
```

f686fb7 Replace debug environment variable with log level 2026-10-04T09:00:00+00:00
</details>

**renamed · line 8** migrate

```diff
- migrate
+ db-upgrade
```

<details><summary>Evidence</summary>

README.md:8

```text
migrate
```

cli.py:1

```text
-    commands.add_parser("migrate")
+    commands.add_parser("db-upgrade")
```

3fb52f1 Rename the database upgrade subcommand 2026-10-05T09:00:00+00:00
</details>

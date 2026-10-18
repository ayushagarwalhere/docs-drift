# Robustness sweep — 2026-10-18-robustness-r2

Random selection and exact pins: repos.json; selection seed 20261021.
Static extraction/index/matching only; no history, models, target execution or correctness assertions.
Acquisition failures, timeouts and crashes are separate outcomes. A non-completed scan is never a pass.
Warning totals cover completed scans only; partial warnings from timed-out scans are unavailable.

| Repo | Status | Seconds | Warnings |
| --- | --- | ---: | ---: |
| rohitg00/ai-engineering-from-scratch | done | 180.4414685999509 | 62 |
| d2l-ai/d2l-zh | done | 5.727619900018908 | 0 |
| scikit-learn/scikit-learn | done | 192.65950439998414 | 0 |
| EbookFoundation/free-programming-books | done | 10.735220000031404 | 0 |
| opendatalab/MinerU | done | 140.8082727000001 | 24 |
| abi/screenshot-to-code | done | 19.65774410002632 | 74 |
| django/django | done | 178.20479029999115 | 4 |
| mvanhorn/last30days-skill | done | 43.288068900001235 | 7 |
| impress/impress.js | done | 5.059519000002183 | 0 |
| gorhill/uBlock | done | 31.670800799969584 | 8 |
| google/zx | done | 6.298353000020143 | 2 |
| DavidHDev/react-bits | done | 168.41983430000255 | 1305 |
| affaan-m/ECC | done | 322.6098641999997 | 9 |
| byoungd/up | done | 9.011582200007979 | 0 |
| trekhleb/javascript-algorithms | done | 5.8169826999655925 | 0 |
| vercel/next.js | done | 1470.5578281999915 | 7171 |
| KeygraphHQ/shannon | done | 2.9146061000064947 | 6 |
| cypress-io/cypress | done | 182.82170489995042 | 464 |
| CherryHQ/cherry-studio | done | 382.13336480001453 | 2964 |
| tldraw/tldraw | done | 168.3601095000049 | 920 |
| n8n-io/n8n | timeout | None | None |
| shadcn-ui/ui | done | 235.052550399967 | 3663 |
| socketio/socket.io | done | 21.890720900031738 | 7 |
| go-gorm/gorm | done | 1.250382800004445 | 0 |
| junegunn/fzf | done | 1.7656616999884136 | 0 |
| netdata/netdata | done | 176.32538500003284 | 39 |
| milvus-io/milvus | done | 466.60258669999894 | 8 |
| netbirdio/netbird | done | 24.086645399976987 | 103 |
| iawia002/lux | done | 3.024501900014002 | 1 |
| mudler/LocalAI | done | 93.32424600003287 | 23 |

## Warning groups

| Message | Count |
| --- | ---: |
| Tree-sitter syntax errors: recognizable declarations only | 15207 |
| Source skipped: configuration nesting/line parser limit | 910 |
| Skipped minified source | 579 |
| Source skipped: non-regular, external, missing or over 1 MB | 104 |
| <location>: multiple nearby default subjects; skipped | 34 |
| <location>: multiple port subjects; default skipped | 15 |
| Cannot read documentation | 9 |
| Cannot list tracked documentation with Git | 3 |
| Could not process individual file | 2 |
| Skipped Python source: syntax or decoding error | 1 |

Exact warning examples and source hashes are retained in robustness.json and measurements.json.
Sparse acquisition can omit binary/Go/Rust paths. Missing-file warnings can reflect acquisition limits; do not describe every such warning as a parser bug.

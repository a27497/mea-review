# Third-party notices

The Review v3 workspace serves two unmodified Latin WOFF2 files locally.
Both are under the SIL Open Font License 1.1.

| Font | Distribution | Copyright and license |
| --- | --- | --- |
| Inter | Google Fonts v20 | [Inter OFL](web/review-font/INTER-OFL.txt) |
| IBM Plex Mono | Google Fonts v20 | [IBM Plex Mono OFL](web/review-font/PLEX-OFL.txt) |

Upstream font projects are [Inter](https://github.com/rsms/inter) and
[IBM Plex](https://github.com/IBM/plex).

Browser test dependencies are installed from the pinned npm lockfile.
Playwright 1.56.1 is Apache-2.0. Its optional macOS dependency fsevents
2.3.2 is MIT. The MCP interoperability check uses the official TypeScript SDK
(`@modelcontextprotocol/sdk` 1.32.2, MIT) as a development dependency. Package
dependencies and browser binaries are downloaded separately.

The workbench examples are synthetic MEA project fixtures. The separate
ReviewBench replay also includes two frozen external PR snapshots used as
inert data. The source notices were checked against both pinned revisions.
The [sample manifest](benchmarks/reviewbench/samples/manifest.json) records
the exact commits, source hashes and license paths.

| ReviewBench material | License |
| --- | --- |
| resplab/leap #48 frozen source and patch | [MIT notice](benchmarks/reviewbench/samples/licenses/leap-MIT.txt) |
| torchfont/torchfont #133 frozen source and patch | [MIT notice](benchmarks/reviewbench/samples/licenses/torchfont-MIT.txt) |
| Pinned ReviewBench input reader and types | [MIT notice](benchmarks/reviewbench/vendor/ReviewBench/LICENSE) |

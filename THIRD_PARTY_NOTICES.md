# Third-party notices

Browser assets under web/vendor/ retain their upstream licenses; the RepoOnboard MIT license does not relicense them. THIRD_PARTY_COMPONENTS.json records the Markmap and AntV G6 bundle inventories separately, with exact package versions, license identifiers, registry records, and source repositories. Full standard license texts are in THIRD_PARTY_LICENSES.md.

| Asset / component | Version | License | Source / notice |
|---|---:|---|---|
| Vue global build | 3.5.13 | MIT | https://github.com/vuejs/core |
| D3 bundle | 7.9.0 | ISC | https://github.com/d3/d3 |
| Markmap markmap-lib and markmap-view browser bundles | 0.18.12 | MIT | https://github.com/markmap/markmap |
| Markmap workspace packages in the bundle graph: markmap-common; markmap-html-parser | 0.18.9; 0.18.11 | MIT | https://github.com/markmap/markmap |
| @gera2ld/jsx-dom embedded in Markmap | 2.2.2 | ISC in npm metadata and bundle banner; published LICENSE says MIT | https://github.com/gera2ld/jsx-dom |
| AntV G6 browser bundle | 4.8.23 | MIT for G6 plus 39 source-mapped entries: 28 MIT, 3 ISC, 6 BSD-3-Clause, 2 0BSD | https://github.com/antvis/G6 |

## Markmap version and dependency evidence

The local markmap-lib.js and markmap-view.js files match the versioned 0.18.12 upstream browser files byte for byte. Their SHA-256 hashes and exact upstream URLs are recorded in THIRD_PARTY_COMPONENTS.json. The 81-row Markmap inventory contains the four Markmap workspace packages and the 77-package production dependency closure, including D3 and KaTeX. The exact dependency resolutions come from the pnpm lockfile at Markmap commit 205367a24603dc187f67da1658940c6cade20dce.

The @gera2ld/jsx-dom 2.2.2 npm metadata and bundled banner say ISC, while its published LICENSE file says MIT. Both standard license texts are included, and the mismatch is disclosed rather than silently choosing one.

## AntV G6 version and dependency evidence

The shipped `web/vendor/g6.min.js` and its source map are the official AntV 4.8.23 CDN files. The JavaScript file's SHA-256 is `8d7970f971daff8727b16cd50890eaed94135a60a5600b7be7918c1031bff4c4`; it matches the 4.8.23 NPM distribution asset. The source map is included beside the bundle so the package/version evidence can be inspected. Its 744 `node_modules` source entries resolve to 39 package-version records; `dagre` and `graphlib` versions were read from their mapped `version.js` source. The JSON also records the top-level `@antv/g6` package, for 40 records total. Exact-version NPM metadata supplied the license identifiers and repository links. See the `antv_g6` section in THIRD_PARTY_COMPONENTS.json for the source-map hash and each component record.

Python dependencies are installed from requirements.txt rather than vendored; their licenses remain with their respective upstream projects.

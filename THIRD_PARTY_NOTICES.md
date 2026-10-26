# Third-party notices

The browser assets in web/vendor/ are redistributed under their upstream terms. The RepoOnboard license does not replace those terms. Common MIT and ISC license texts are recorded in THIRD_PARTY_LICENSES.md; copyright holders and available upstream references are listed below.

| Asset | Version identifiable in supplied files | License | Copyright / attribution | Upstream |
|---|---:|---|---|---|
| Vue global build | 3.5.13 | MIT | Yuxi (Evan) You | https://github.com/vuejs/core |
| D3 | 7.9.0 | ISC | Mike Bostock | https://github.com/d3/d3 |
| Markmap markmap-lib / markmap-view bundles | Not recorded | MIT | Gerald (upstream project notice) | https://github.com/markmap/markmap |
| @gera2ld/jsx-dom embedded in Markmap | 2.2.2 | ISC in bundle banner and npm metadata; package LICENSE says MIT | Gerald | https://github.com/gera2ld/jsx-dom |
| AntV G6 | 4.8.23 | MIT | Alipay.inc | https://github.com/antvis/G6 |
| AntV G renderer packages embedded in G6 | g-base 0.5.1; g-canvas / g-svg 0.5.2 | ISC | AntV contributors (upstream package author metadata) | https://github.com/antvis/G |
| regenerator-runtime embedded in G6 | Not recorded | MIT | Facebook, Inc. | https://github.com/facebook/regenerator |
| KaTeX resources referenced by Markmap | 0.16.18 URL reference; files are not bundled here | MIT upstream | Khan Academy and contributors | https://github.com/KaTeX/KaTeX |

The original vendor directory has no lockfile or complete component inventory. The Markmap bundle version and full transitive license set could not be recovered from the supplied artifacts. The table reports identifiable components and must not be read as a complete software bill of materials. The @gera2ld/jsx-dom metadata/banner and package LICENSE disagree on the license identifier; both MIT and ISC text are included here. Python dependencies are installed from requirements.txt rather than vendored; their licenses remain with their respective upstream projects.

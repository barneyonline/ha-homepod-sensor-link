# Wiki banner artwork

Each of the 11 Wiki pages has a light and dark banner based on the approved README Smoked Midnight design. The rounded opaque midnight speaker, cyan/violet lighting and external glass tiles form the shared visual template. The heading, subtitle and external pictograms identify each page.

Generated with the built-in OpenAI image generation tool using the corresponding README theme as an editing reference. Full prompts and reference paths are recorded in [prompts.json](prompts.json); [banners.json](banners.json) records the page catalogue. These are illustrative project artwork, not screenshots, Apple assets or evidence of compatibility or certification.

Generation masters use the template's 2172 × 724 canvas. Publication files are opaque 1800 × 600 JPEGs exported with macOS `sips`, quality 86, preserving the 3:1 composition. This keeps the complete theme set suitable for documentation downloads. Only publication JPEGs are included in manual-install archives.

`scripts/build_wiki.py` adds accessible theme-aware picture headers and copies the exact publication bytes into a separate GitHub Wiki export outside the source tree. Exporting does not call an image service or access the network. Add a matching pair when adding a page, then rebuild the export. Keep the canonical page heading and text available independently of its banner.
